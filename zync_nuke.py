"""
Zync Nuke Plugin

This package provides a Python-based Nuke plugin
for launching jobs to the Zync Render Platform.

Usage as a menu item:
  nuke.pluginAddPath('/path/to/zync-nuke')
  import zync_nuke
  menu.addCommand('Render on Zync', 'zync_nuke.submit_dialog()')
"""

import hashlib
import nuke
import nukescripts
import platform
import os
import re
import socket
import sys
import time
import traceback
import urllib

if os.environ.get('ZYNC_API_DIR') and os.environ.get('ZYNC_NUKE_API_KEY'):
  API_DIR = os.environ.get('ZYNC_API_DIR')
  API_KEY = os.environ.get('ZYNC_NUKE_API_KEY')
else:
  config_path = os.path.join(os.path.dirname(__file__), 'config_nuke.py')
  if not os.path.exists(config_path):
    raise Exception('Could not locate config_nuke.py, please create.')
  from config_nuke import *

required_config = ['API_DIR', 'API_KEY']

for key in required_config:
  if not key in globals():
    raise Exception('config_nuke.py must define a value for %s.' % (key,))

nuke.pluginAddPath(API_DIR)
import zync

def get_dependent_nodes(root):
  """
  Returns a list of all of the root node's dependencies.
  Uses `nuke.dependencies()`. This will work with nested
  dependencies.
  """
  all_deps = set([root])
  all_deps.update(nuke.dependencies(list(all_deps)))

  seen = set()
  while True:
    diff = all_deps - seen
    to_add = nuke.dependencies(list(diff))
    all_deps.update(to_add)
    seen.update(diff)
    if len(diff) == 0:
      break

  return list(all_deps)

def select_deps(nodes):
  """
  Selects all of the dependent nodes for the given list of nodes.
  """
  for node in nodes:
    for node in get_dependent_nodes(node):
      node.setSelected(True)

def freeze_stereo_node(node, view=None):
  """
  Freezes the given stereo node, removes any expressions and creates a L/R
  """
  freeze_node(node)

  if view:
    file_name = node.knob('file').value()
    file_name = file_name.replace('%v', view.lower())
    file_name = file_name.replace('%V', view.upper())

    node.knob('file').setValue(file_name)

def freeze_node(node, view=None):
  """
  If the node has an expression, evaluate it so that Zync receives a file
  path it can understand. Accounts for and retains frame number expressions.
  """
  knob_names = ['file', 'font']
  for knob_name in knob_names:
    knob = node.knob(knob_name)
    if knob == None:
      continue
    knob_value = knob.value()
    # If the value returned is not a string, do not continue.
    if not isinstance(knob_value, basestring):
      continue
    # If the knob value has an open bracket, assume it's an expression.
    if '[' in knob_value:
      if node.Class() == 'Write':
        knob.setValue(nuke.filename(node))
      else:
        # Running knob.evaluate() will freeze not just expressions, but
        # frame number as well. Use regex to search for any frame number
        # expressions, and replace them with a placeholder.
        to_eval = knob_value
        placeholders = {}
        regexs = [
          r'#+',
          r'%.*d'
        ]
        for regex in regexs:
          match = 1
          while match:
            match = re.search(regex, to_eval)
            if match:
              placeholder = '__frame%d' % (len(placeholders)+1,)
              original = match.group()
              placeholders[placeholder] = original
              to_eval = (to_eval[0:match.start()] + '{%s}' % (placeholder,) +
                to_eval[match.end():])
        # Set the knob value to our string with placeholders.
        knob.setValue(to_eval)
        # Now evaluate the knob to freeze the path.
        frozen_path = knob.evaluate()
        # Use our dictionary of placeholders to place the original frame
        # number expressions back in.
        frozen_path = frozen_path.format(**placeholders)
        # Finally, set the frozen path back to the knob.
        knob.setValue(frozen_path)
    # For Write node paths, if the path is relative expand it using the
    # project directory. If no project directory is set, fall back to
    # using the directory in which the Nuke script lives.
    if node.Class() == 'Write':
      if not os.path.isabs(knob.value()):
        project_dir = nuke.root().knob('project_directory').evaluate()
        if not project_dir:
          project_dir = os.path.dirname(nuke.root().knob('name').getValue())
        absolute_path = os.path.abspath(os.path.join(project_dir, knob.value()))
        knob.setValue(absolute_path)
    # If a view was given, replace view expressions with that.
    if view:
      knob_value = knob_value.replace('%v', view.lower())
      knob_value = knob_value.replace('%V', view.upper())
      node.knob(knob_name).setValue(knob_value)

def gizmos_to_groups(nodes):
  """
  If the node is a Gizmo, use makeGroup() to turn it into a Group.
  """
  # deselect all nodes. catch errors for nuke versons that don't
  # support the recurseGroups option.
  try:
    node_list = nuke.allNodes(recurseGroups=True)
  except:
    node_list = nuke.allNodes()
  for node in node_list:
    node.setSelected(False)
  for node in nodes:
    if hasattr(node, 'makeGroup') and callable(getattr(node, 'makeGroup')):
      node.setSelected(True)
      node.makeGroup()
      nuke.delete(node)

def clear_nodes_by_name(names):
  """
  Removes nodes that match any of the names given.
  """
  nodes = (x for x in nuke.allNodes())
  for node in nodes:
    for name in names:
      if name in node.name():
        nuke.delete(node)

def clear_callbacks(node):
  """
  Call and clear the callbacks on the given node

  WARNING: only supports the create_write_dirs callback
  """
  names = ('beforeRender', 'beforeFrameRender', 'afterFrameRender', 'afterRender')
  knobs = (node.knob(x) for x in names)
  for knob in knobs:
    knob_val = knob.value()
    if 'create_write_dirs' in knob_val:
      try:
        create_write_dirs(node)
      except NameError:
        nuke.callbacks.create_write_dirs(node)
      knob.setValue('')

def clear_view(node):
  """
  Sets the node's 'views' knob to left, for maximum ZYNC compatibility.
  """
  if 'views' in node.knobs():
    node.knob('views').setValue('left')

def is_stereo(node):
  """
  If the node is stereo (i.e. has %v or %V in the path)
  """
  path = node.knob('file').value()
  return '%v' in path or '%V' in path

def is_valid(node):
  """
  Checks if the readnode is valid: if it has spaces or apostrophes in the
  name, it's invalid.
  """
  path = node.knob('file').value()
  return ' ' in path or '\'' in path

def stereo_script():
  for read in (x for x in nuke.allNodes() if x.Class() == 'Read'):
    if is_stereo(read):
      return True
  for write in (x for x in nuke.allNodes() if x.Class() == 'Write'):
    if is_stereo(write):
      return True
    if 'left right' == write.knob('views').value():
      return True

  return False

def preflight(view=None):
  """
  Runs a preflight pass on the current nuke scene. Modify as needed.
  Returning True = success, False = failure
  """
  return True

class WriteChanges(object):
  """
  Given a script to save to, will save all of the changes made in the
  with block to the script, then undoes those changes in the current
  script. For example:

  with WriteChanges('/Volumes/af/show/omg/script.nk'):
    for node in nuke.allNodes():
      node.setYpos(100)
  """
  def __init__(self, script, save_func=None):
    """
    Initialize a WriteChanges context manager.
    Must provide a script to write to.

    If you provide a save_func, it will be called instead of the default
    `nuke.scriptSave`. The function must have the same interface as
    `nuke.scriptSave`. A possible alternative is `nuke.nodeCopy`.
    """
    self.undo = nuke.Undo
    self.__disabled = self.undo.disabled()
    self.script = script
    if save_func:
      self.save_func = save_func
    else:
      self.save_func = nuke.scriptSave

  def __enter__(self):
    """
    Enters the with block.
    NOTE: does not return an object, so assigment using 'as' doesn't work:
      `with WriteChanges('foo') as wc:`
    """
    if self.__disabled:
      self.undo.enable()

    self.undo.begin()

  def __exit__(self, type, value, traceback):
    """
    Exits the with block.

    First it calls the save_func, then undoes all actions in the with
    context, leaving the state of the current script untouched.
    """
    self.save_func(self.script)
    self.undo.cancel()
    if self.__disabled:
      self.undo.disable()

class ZyncRenderPanel(nukescripts.panels.PythonPanel):
  def __init__(self):
    # make sure this isn't an unsaved script
    if nuke.root().name() == 'Root' or nuke.modified():
      msg = 'Please save your script before rendering on Zync.'
      raise Exception(msg)

    nukescripts.panels.PythonPanel.__init__(self, 'Zync Render',
      'com.google.zync')

    if platform.system() in ('Windows', 'Microsoft'):
      self.usernameDefault = os.environ['USERNAME']
    else:
      self.usernameDefault = os.environ['USER']

    #GET WRITE NODES FROM FILE
    self.writeDict = dict()
    self.update_write_dict()

    # CREATE KNOBS
    self.num_slots = nuke.Int_Knob('num_slots', 'Num. Machines:')
    self.num_slots.setDefaultValue((1,))

    sorted_types = [t for t in ZYNC.INSTANCE_TYPES]
    sorted_types.sort(ZYNC.compare_instance_types)
    display_list = []
    for inst_type in sorted_types:
      label = '%s (%s)' % (inst_type,
        ZYNC.INSTANCE_TYPES[inst_type]['description'].replace(', preemptible',''))
      inst_type_base = inst_type.split(' ')[-1]
      pricing_key = 'CP-ZYNC-%s-NUKE' % (inst_type_base.upper(),)
      if 'PREEMPTIBLE' in inst_type.upper():
        pricing_key += '-PREEMPTIBLE'
      if (pricing_key in ZYNC.PRICING['gcp_price_list'] and
        'us' in ZYNC.PRICING['gcp_price_list'][pricing_key]):
        label += ' $%s/hr' % (
          ZYNC.PRICING['gcp_price_list'][pricing_key]['us'],)
      display_list.append(label)
    self.instance_type = nuke.Enumeration_Knob('instance_type', 'Type:',
      display_list)

    self.pricing_label = nuke.Text_Knob('pricing_label', '')
    self.pricing_label.setValue('Est. Cost per Hour: Not Available')

    calculator_link = nuke.Text_Knob('calculator_link', '')
    calculator_link.setValue('<a style="color:#ff8a00;" ' +
      'href="http://zync.cloudpricingcalculator.appspot.com">' +
      'Cost Calculator</a>')

    proj_response = ZYNC.get_project_list()
    self.existing_project = nuke.Enumeration_Knob('existing_project',
      'Existing Project:', [' '] + [p['name'] for p in proj_response])

    self.new_project = nuke.String_Knob('project', ' New Project:')
    self.new_project.clearFlag(nuke.STARTLINE)

    self.upload_only = nuke.Boolean_Knob('upload_only', 'Upload Only')
    self.upload_only.setFlag(nuke.STARTLINE)

    self.parent_id = nuke.String_Knob('parent_id', 'Parent ID:')
    self.parent_id.setValue('')

    # create shotgun controls - they'll only be added if shotgun integration
    # is enabled.
    self.sg_create_version = nuke.Boolean_Knob('sg_create_version',
      'Create Shotgun Version')
    self.sg_create_version.setFlag(nuke.STARTLINE)
    self.sg_create_version.setValue(False)
    self.sg_user = nuke.String_Knob('sg_user', 'Shotgun User:')
    self.sg_user.setFlag(nuke.STARTLINE)
    self.sg_project = nuke.String_Knob('sg_project', 'Shotgun Project:')
    self.sg_project.setFlag(nuke.STARTLINE)
    self.sg_shot = nuke.String_Knob('sg_shot', 'Shotgun Shot:')
    self.sg_shot.setFlag(nuke.STARTLINE)
    self.sg_version_code = nuke.String_Knob('sg_version_code', 'Version Code:')
    self.sg_version_code.setFlag(nuke.STARTLINE)
    script_base, ext = os.path.splitext(os.path.basename(
      nuke.root().knob('name').getValue()))
    self.sg_version_code.setValue(script_base)
    self.hideSGControls()

    self.priority = nuke.Int_Knob('priority', 'Job Priority:')
    self.priority.setDefaultValue((50,))

    self.skip_check = nuke.Boolean_Knob('skip_check', 'Skip File Check')
    self.skip_check.setFlag(nuke.STARTLINE)

    first = nuke.root().knob('first_frame').value()
    last = nuke.root().knob('last_frame').value()
    frange = '%d-%d' % (first, last)
    self.frange = nuke.String_Knob('frange', 'Frame Range:', frange)

    self.fstep = nuke.Int_Knob('fstep', 'Frame Step:')
    self.fstep.setDefaultValue((1,))

    selected_write_nodes = []
    for node in nuke.selectedNodes():
      if node.Class() == 'Write':
        selected_write_nodes.append(node.name())
    self.writeNodes = []
    colNum = 1
    for writeName in self.writeListNames:
      knob = nuke.Boolean_Knob(writeName, writeName)
      if len(selected_write_nodes) == 0:
        knob.setValue(True)
      elif writeName in selected_write_nodes:
        knob.setValue(True)
      else:
        knob.setValue(False)
      if colNum == 1:
        knob.setFlag(nuke.STARTLINE)
      if colNum > 3:
        colNum = 1
      else:
        colNum += 1
      knob.setTooltip(self.writeDict[writeName].knob('file').value())
      self.writeNodes.append(knob)

    self.chunk_size = nuke.Int_Knob('chunk_size', 'Chunk Size:')
    self.chunk_size.setDefaultValue((10,))

    # controls for logging in and out
    self.loginButton = nuke.Script_Knob('login', 'Login With Google')
    self.logoutButton = nuke.Script_Knob('logout', 'Logout')
    # keep everything on the same line
    self.logoutButton.clearFlag(nuke.STARTLINE)
    self.userLabel = nuke.Text_Knob('user_label', '')
    # set value to whitespace, otherwise Nuke draws an unsightly line
    # through the element
    self.userLabel.setValue(' ')
    self.userLabel.clearFlag(nuke.STARTLINE)

    # these buttons must be named okButton and cancelButton for Nuke
    # to add default OK/Cancel functionality. if named something else,
    # Nuke will add its own default buttons.
    self.okButton = nuke.Script_Knob('submit', 'Submit Job')
    self.cancelButton = nuke.Script_Knob('cancel', 'Cancel')

    # ADD KNOBS
    self.addKnob(self.num_slots)
    self.addKnob(self.instance_type)
    self.addKnob(self.pricing_label)
    self.addKnob(calculator_link)
    self.addKnob(self.__getDivider())
    self.addKnob(self.existing_project)
    self.addKnob(self.new_project)
    self.addKnob(self.parent_id)
    if 'shotgun' in ZYNC.FEATURES and ZYNC.FEATURES['shotgun'] == 1:
      self.addKnob(self.sg_create_version)
      self.addKnob(self.sg_user)
      self.addKnob(self.sg_project)
      self.addKnob(self.sg_shot)
      self.addKnob(self.sg_version_code)
    self.addKnob(self.upload_only)
    self.addKnob(self.priority)
    self.addKnob(self.skip_check)
    self.addKnob(self.frange)
    self.addKnob(self.fstep)
    for k in self.writeNodes:
      self.addKnob(k)
    self.addKnob(self.chunk_size)
    self.addKnob(self.__getDivider())
    self.addKnob(self.loginButton)
    self.addKnob(self.logoutButton)
    self.addKnob(self.userLabel)
    self.addKnob(self.__getDivider())
    self.addKnob(self.okButton)
    self.addKnob(self.cancelButton)

    # collect render-specific knobs for iterating on later
    self.render_knobs = (self.num_slots, self.instance_type,
      self.frange, self.fstep, self.chunk_size, self.skip_check,
      self.priority, self.parent_id)

    if 'shotgun' in ZYNC.FEATURES and ZYNC.FEATURES['shotgun'] == 1:
      height = 510
    else:
      height = 410
    self.setMinimumSize(600, height)

    self.update_pricing_label()

  def __getDivider(self):
    """Get a divider, a horizontal line used for organizing UI elements."""
    return nuke.Text_Knob('divider', '', '')

  def update_write_dict(self):
    wd = dict()
    for node in (x for x in nuke.allNodes() if x.Class() == 'Write'):
      # only put nodes that are not disabled in the write dict
      if not node.knob('disable').value():
        wd[node.name()] = node

    self.writeDict.update(wd)
    self.writeListNames = self.writeDict.keys()
    self.writeListNames.sort()

  def get_params(self):
    """
    Returns a dictionary of the job parameters from the submit render gui.
    """
    params = dict()
    params['num_instances'] = self.num_slots.value()

    for inst_type in ZYNC.INSTANCE_TYPES:
      if self.instance_type.value().startswith(inst_type):
        params['instance_type'] = inst_type

    # these fields can't both be blank, we check in submit() before
    # reaching this point
    params['proj_name'] = self.existing_project.value().strip()
    if params['proj_name'] == '':
      params['proj_name'] = self.new_project.value().strip()

    params['frange'] = self.frange.value()
    params['step'] = self.fstep.value()
    params['chunk_size'] = self.chunk_size.value()
    params['upload_only'] = int(self.upload_only.value())
    params['priority'] = int(self.priority.value())
    parent = self.parent_id.value()
    if parent != None and parent != '':
      params['parent_id'] = int(self.parent_id.value())

    params['start_new_instances'] = '1'
    params['skip_check'] = '1' if self.skip_check.value() else '0'
    params['notify_complete'] = '0'

    if ('shotgun' in ZYNC.FEATURES and ZYNC.FEATURES['shotgun'] == 1
      and self.sg_create_version.value()):
      params['sg_user'] = self.sg_user.value()
      params['sg_project'] = self.sg_project.value()
      params['sg_shot'] = self.sg_shot.value()
      params['sg_version_code'] = self.sg_version_code.value()

    return params

  def submit_checks(self):
    """Check current settings and raise errors for anything that
    could cause problems when submitting the job.

    Raises:
      zync.ZyncError for any issues found
    """
    if not ZYNC.has_user_login():
      raise zync.ZyncError('Please login before submitting a job.')

    if self.existing_project.value().strip() == '' and self.new_project.value().strip() == '':
      raise zync.ZyncError('Project name cannot be blank. Please either choose ' +
        'an existing project from the dropdown or enter the desired ' +
        'project name in the New Project field.')

    if self.skip_check.value():
      skip_answer = nuke.ask('You\'ve asked Zync to skip the file check ' +
        'for this job. If you\'ve added new files to your script this ' +
        'job WILL error. Your nuke script will still be uploaded. Are ' +
        'you sure you want to continue?')
      if not skip_answer:
        raise zync.ZyncError('Job submission canceled.')

  def submit(self):
    """
    Does the work to submit the current Nuke script to Zync,
    given that the parameters on the dialog are set.
    """

    selected_write_names = []
    selected_write_nodes = []
    for k in self.writeNodes:
      if k.value():
        selected_write_names.append(k.label())
        selected_write_nodes.append(nuke.toNode(k.label()))

    active_viewer = nuke.activeViewer()
    if active_viewer:
      viewer_input = active_viewer.activeInput()
      if viewer_input == None:
        viewed_node = None
      else:
        viewed_node = active_viewer.node().input(viewer_input)
    else:
      viewer_input, viewed_node = None, None

    new_script = ZYNC.generate_file_path(nuke.root().knob('name').getValue())
    with WriteChanges(new_script):
      # The WriteChanges context manager allows us to save the
      # changes to the current session to the given script, leaving
      # the current session unchanged once the context manager is
      # exited.
      preflight_result = preflight()

      #
      #   Nuke 7.0v1 through 7.0v8 broke its own undo() functionality, so this will only
      #   run on versions other than those.
      #
      if nuke.NUKE_VERSION_MAJOR != 7 or nuke.NUKE_VERSION_MINOR > 0 or nuke.NUKE_VERSION_RELEASE > 8:
        #
        #   Remove all nodes that aren't connected to the Write
        #   nodes being rendered.
        #
        select_deps(selected_write_nodes)
        for node in nuke.allNodes():
          if node.isSelected():
            node.setSelected(False)
          else:
            node.setSelected(True)
        nuke.nodeDelete()
        #
        #   Freeze expressions on all nodes. Catch errors for Nuke
        #   versions that don't support the recurseGroups option.
        #
        try:
          node_list = nuke.allNodes(recurseGroups=True)
        except:
          node_list = nuke.allNodes()
        for node in node_list:
          freeze_node(node)

    if not preflight_result:
      return

    # reconnect the viewer
    if viewer_input != None and viewed_node != None:
      nuke.connectViewer(viewer_input, viewed_node)

    # exec before render
    #nuke.callbacks.beforeRenders

    try:
      render_params = self.get_params()
      if render_params == None:
        return
      ZYNC.submit_job('nuke', new_script, ','.join(selected_write_names), render_params)
    except zync.ZyncPreflightError as e:
      raise Exception('Preflight Check Failed:\n\n%s' % (str(e),))

    nuke.message('Job submitted to ZYNC.')

  def knobChanged(self, knob):
    """Handles knob callbacks."""
    # "submit job" button
    if knob is self.okButton:
      # run presubmit checks to make sure the job is ready to be
      # launched with the currently selected parameters. we do
      # this here so we can display errors to the user before
      # the dialog closes and destroys all of their settings. we
      # cannot do the full job submission here though, because
      # trying to use nuke.Undo functionality while a modal dialog
      # is open crashes Nuke.
      try:
        self.submit_checks()
      # raised exceptions will automatically cause Nuke to abort
      # and leave the dialog open. we just capture that and show
      # a message to the user so they know what went wrong. the
      # full exception will be printed to the Script Editor for
      # further debugging.
      except Exception as e:
        nuke.message(str(e))
        raise
    elif knob is self.loginButton:
      # run the auth flow, and display the user's email address,
      # adding a little whitespace padding for visual clarity.
      self.userLabel.setValue('  %s' % ZYNC.login_with_google())
    elif knob is self.logoutButton:
      ZYNC.logout()
      self.userLabel.setValue('')
    elif knob is self.upload_only:
      checked = self.upload_only.value()
      for rk in self.render_knobs:
        rk.setEnabled(not checked)
      for k in self.writeNodes:
        k.setEnabled(not checked)
    elif knob is self.sg_create_version:
      checked = self.sg_create_version.value()
      if checked:
        self.showSGControls()
      else:
        self.hideSGControls()
    elif knob is self.num_slots or knob is self.instance_type:
      self.update_pricing_label()

  def showModalDialog(self):
    """
    Shows the Zync Submit dialog and does the work to submit it.
    """
    if nukescripts.panels.PythonPanel.showModalDialog(self):
      self.submit()

  def hideSGControls(self):
    self.sg_user.setEnabled(False)
    self.sg_project.setEnabled(False)
    self.sg_shot.setEnabled(False)
    self.sg_version_code.setEnabled(False)
  def showSGControls(self):
    self.sg_user.setEnabled(True)
    self.sg_project.setEnabled(True)
    self.sg_shot.setEnabled(True)
    self.sg_version_code.setEnabled(True)

  def update_pricing_label(self):
    machine_type = self.instance_type.value().split(' (')[0]
    num_machines = self.num_slots.value()
    machine_type_base = machine_type.split(' ')[-1]
    field_name = 'CP-ZYNC-%s-NUKE' % (machine_type_base.upper(),)
    if 'PREEMPTIBLE' in machine_type.upper():
      field_name += '-PREEMPTIBLE'
    if (field_name in ZYNC.PRICING['gcp_price_list'] and
      'us' in ZYNC.PRICING['gcp_price_list'][field_name]):
      cost = '$%.02f' % ((float(num_machines) *
        ZYNC.PRICING['gcp_price_list'][field_name]['us']),)
    else:
      cost = 'Not Available'
    self.pricing_label.setValue('Est. Cost per Hour: %s' % (cost,))

def submit_dialog():
  global ZYNC
  ZYNC = zync.Zync('nuke_plugin', API_KEY)
  ZyncRenderPanel().showModalDialog()
