"""Zync Nuke Plugin

This package provides a Python-based Nuke plugin
for launching jobs to the Zync Render Platform.

Usage as a menu item:
  nuke.pluginAddPath('/path/to/zync-nuke')
  import zync_nuke
  menu.addCommand('Render on Zync', 'zync_nuke.submit_dialog()')
"""

import nuke
import nukescripts
import platform
import os
import re

__version__ = '1.3.3'

READ_NODE_CLASSES = ['AudioRead', 'Axis', 'Axis2', 'Camera', 'Camera2', 'DeepRead', 'OCIOFileTransform',
                     'ParticleCache', 'Precomp', 'Read', 'ReadGeo', 'ReadGeo2', 'Vectorfield' ]
WRITE_NODE_CLASSES = ['DeepWrite', 'GenerateLUT', 'Write', 'WriteGeo']
PATH_KNOB_NAMES = ['proxy', 'file', 'vfield_file']


if os.environ.get('ZYNC_API_DIR'):
  API_DIR = os.environ.get('ZYNC_API_DIR')
else:
  config_path = os.path.join(os.path.dirname(__file__), 'config_nuke.py')
  if not os.path.exists(config_path):
    raise Exception('Could not locate config_nuke.py, please create.')
  from config_nuke import *

required_config = ['API_DIR']

for key in required_config:
  if not key in globals():
    raise Exception('config_nuke.py must define a value for %s.' % (key,))

nuke.pluginAddPath(API_DIR)
import zync


def get_dependent_nodes(root):
  """Returns a list of all of the root node's dependencies.

  Uses `nuke.dependencies()`. This will work with nested dependencies.
  """
  all_deps = {root}
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
  """Selects all of the dependent nodes for the given list of nodes."""
  for node in nodes:
    for node in get_dependent_nodes(node):
      node.setSelected(True)


def freeze_node(node, view=None):
  """If the node has an expression, evaluate it so that Zync receives a file path it can understand.

  Accounts for and retains frame number expressions.
  """

  def is_knob_rewritable(knob):
    return knob is not None and isinstance(knob.value(), basestring) and knob.value()

  node_classes_to_absolutize = ['AudioRead', 'Axis', 'Axis2' 'Camera', 'Camera2' 'DeepRead', 'DeepWrite',
                                'GenerateLUT', 'OCIOFileTransform', 'ParticleCache',
                                'Precomp', 'Read', 'ReadGeo', 'ReadGeo2', 'Vectorfield',
                                'Write', 'WriteGeo']

  for knob_name in PATH_KNOB_NAMES:
    knob = node.knob(knob_name)
    if is_knob_rewritable(knob):
      _evaluate_path_expression(node, knob)
      if node.Class() in node_classes_to_absolutize:
        # Nuke scene can have file paths relative to project directory
        _maybe_absolutize_path(knob)
      if view:
        _expand_view_tokens_in_path(knob, view)
      _clean_path(knob)


def _evaluate_path_expression(node, knob):
  knob_value = knob.value()
  # If the knob value has an open bracket, assume it's an expression.
  if '[' in knob_value:
    if node.Class() in WRITE_NODE_CLASSES:
      knob.setValue(nuke.filename(node))
    else:
      # Running knob.evaluate() will freeze not just expressions, but frame number as well. Use regex to search for
      # any frame number expressions, and replace them with a placeholder.
      to_eval = knob_value
      placeholders = {}
      regexs = [r'#+', r'%.*d']
      for regex in regexs:
        match = 1
        while match:
          match = re.search(regex, to_eval)
          if match:
            placeholder = '__frame%d' % (len(placeholders) + 1,)
            original = match.group()
            placeholders[placeholder] = original
            to_eval = to_eval[0:match.start()] + '{%s}' % (placeholder,) + to_eval[match.end():]
      # Set the knob value to our string with placeholders.
      knob.setValue(to_eval)
      # Now evaluate the knob to freeze the path.
      frozen_path = knob.evaluate()
      # Use our dictionary of placeholders to place the original frame number expressions back in.
      frozen_path = frozen_path.format(**placeholders)
      # Finally, set the frozen path back to the knob.
      knob.setValue(frozen_path)


def _maybe_absolutize_path(knob):
  if not os.path.isabs(knob.value()):
    project_dir = _get_project_directory()
    absolute_path = os.path.abspath(os.path.join(project_dir, knob.value()))
    knob.setValue(absolute_path)


def _get_project_directory():
  project_dir = nuke.root().knob('project_directory').evaluate()
  if not project_dir:
    # When no project dir is set, return the dir in which Nuke scene lives
    project_dir = os.path.dirname(nuke.root().knob('name').getValue())
  return project_dir


def _expand_view_tokens_in_path(knob, view):
  view_expanded_path = knob.value()
  # Token %v is replaced with the first letter of the view name
  view_expanded_path = view_expanded_path.replace('%v', view[0])
  # Token %V is replaced with the full name of the view
  view_expanded_path = view_expanded_path.replace('%V', view)
  knob.setValue(view_expanded_path)


def _clean_path(knob):
  path = knob.value()
  path = path.replace('\\', '/')
  knob.setValue(path)


def gizmos_to_groups(nodes):
  """If the node is a Gizmo, use makeGroup() to turn it into a Group."""
  # Deselect all nodes. catch errors for nuke versons that don't support the recurseGroups option.
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


class WriteChanges(object):
  """Given a script to save to, will save all of the changes made in the with block to the script,

  then undoes those changes in the current script.

  For example:
  with WriteChanges('/Volumes/af/show/omg/script.nk'):
    for node in nuke.allNodes():
      node.setYpos(100)
  """

  def __init__(self, script, save_func=None):
    """Initialize a WriteChanges context manager.

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
    """Enters the with block.

    NOTE: does not return an object, so assigment using 'as' doesn't work:
      `with WriteChanges('foo') as wc:`
    """
    if self.__disabled:
      self.undo.enable()

    self.undo.begin()

  def __exit__(self, type, value, traceback):
    """Exits the with block.

    First it calls the save_func, then undoes all actions in the with
    context, leaving the state of the current script untouched.
    """
    self.save_func(self.script)
    self.undo.cancel()
    if self.__disabled:
      self.undo.disable()


class ZyncRenderPanel(nukescripts.panels.PythonPanel):

  def __init__(self):
    if nuke.root().name() == 'Root' or nuke.modified():
      msg = 'Please save your script before rendering on Zync.'
      raise Exception(msg)

    self.zync_conn = zync.Zync(application='nuke')

    nukescripts.panels.PythonPanel.__init__(self, 'Zync Render', 'com.google.zync')

    if platform.system() in ('Windows', 'Microsoft'):
      self.usernameDefault = os.environ['USERNAME']
    else:
      self.usernameDefault = os.environ['USER']

    # Get write nodes from scene
    self.writeListNames = []
    self.writeDict = dict()
    self.update_write_dict()

    # Create UI knobs
    self.num_slots = nuke.Int_Knob('num_slots', 'Num. Machines:')
    self.num_slots.setDefaultValue((1,))

    sorted_types = [t for t in self.zync_conn.INSTANCE_TYPES]
    sorted_types.sort(self.zync_conn.compare_instance_types)
    display_list = []
    for inst_type in sorted_types:
      inst_desc = self.zync_conn.INSTANCE_TYPES[inst_type]['description'].replace(', preemptible', '')
      label = '%s (%s)' % (inst_type, inst_desc)
      inst_type_base = inst_type.split(' ')[-1]
      pricing_key = 'CP-ZYNC-%s-NUKE' % (inst_type_base.upper(),)
      if 'PREEMPTIBLE' in inst_type.upper():
        pricing_key += '-PREEMPTIBLE'
      if (pricing_key in self.zync_conn.PRICING['gcp_price_list'] and 'us' in self.zync_conn.PRICING['gcp_price_list'][
        pricing_key]):
        label += ' $%s/hr' % (self.zync_conn.PRICING['gcp_price_list'][pricing_key]['us'],)
      display_list.append(label)
    self.instance_type = nuke.Enumeration_Knob('instance_type', 'Type:', display_list)

    self.pricing_label = nuke.Text_Knob('pricing_label', '')
    self.pricing_label.setValue('Est. Cost per Hour: Not Available')

    calculator_link = nuke.Text_Knob('calculator_link', '')
    calculator_link.setValue('<a style="color:#ff8a00;" ' +
                             'href="http://zync.cloudpricingcalculator.appspot.com">Cost Calculator</a>')

    proj_response = self.zync_conn.get_project_list()
    existing_projects = [' '] + [p['name'] for p in proj_response]
    self.existing_project = nuke.Enumeration_Knob('existing_project', 'Existing Project:', existing_projects)

    self.new_project = nuke.String_Knob('project', ' New Project:')
    self.new_project.clearFlag(nuke.STARTLINE)

    self.upload_only = nuke.Boolean_Knob('upload_only', 'Upload Only')
    self.upload_only.setFlag(nuke.STARTLINE)

    self.parent_id = nuke.String_Knob('parent_id', 'Parent ID:')
    self.parent_id.setValue('')

    self.priority = nuke.Int_Knob('priority', 'Job Priority:')
    self.priority.setDefaultValue((50,))

    self.skip_check = nuke.Boolean_Knob('skip_check', 'Skip File Sync')
    self.skip_check.setFlag(nuke.STARTLINE)

    first = nuke.root().knob('first_frame').value()
    last = nuke.root().knob('last_frame').value()
    frange = '%d-%d' % (first, last)
    self.frange = nuke.String_Knob('frange', 'Frame Range:', frange)

    self.fstep = nuke.Int_Knob('fstep', 'Frame Step:')
    self.fstep.setDefaultValue((1,))

    selected_write_nodes = []
    for node in nuke.selectedNodes():
      if node.Class() in WRITE_NODE_CLASSES:
        selected_write_nodes.append(node.name())
    self.writeNodes = []
    col_num = 1
    for writeName in self.writeListNames:
      knob = nuke.Boolean_Knob(writeName, writeName)
      if len(selected_write_nodes) == 0:
        knob.setValue(True)
      elif writeName in selected_write_nodes:
        knob.setValue(True)
      else:
        knob.setValue(False)
      if col_num == 1:
        knob.setFlag(nuke.STARTLINE)
      if col_num > 3:
        col_num = 1
      else:
        col_num += 1
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
    self.userLabel.setValue('  %s' % self.zync_conn.email)
    self.userLabel.clearFlag(nuke.STARTLINE)

    # these buttons must be named okButton and cancelButton for Nuke to add default OK/Cancel functionality.
    # if named something else, Nuke will add its own default buttons.
    self.okButton = nuke.Script_Knob('submit', 'Submit Job')
    self.cancelButton = nuke.Script_Knob('cancel', 'Cancel')

    self.addKnob(self.num_slots)
    self.addKnob(self.instance_type)
    self.addKnob(self.pricing_label)
    self.addKnob(calculator_link)
    self.addKnob(ZyncRenderPanel._get_divider())
    self.addKnob(self.existing_project)
    self.addKnob(self.new_project)
    self.addKnob(self.parent_id)
    self.addKnob(self.upload_only)
    self.addKnob(self.priority)
    self.addKnob(self.skip_check)
    self.addKnob(self.frange)
    self.addKnob(self.fstep)
    for k in self.writeNodes:
      self.addKnob(k)
    self.addKnob(self.chunk_size)
    self.addKnob(ZyncRenderPanel._get_divider())
    self.addKnob(self.loginButton)
    self.addKnob(self.logoutButton)
    self.addKnob(self.userLabel)
    self.addKnob(ZyncRenderPanel._get_divider())
    self.addKnob(self.okButton)
    self.addKnob(self.cancelButton)

    # Collect render-specific knobs for iterating on later
    self.render_knobs = (
    self.num_slots, self.instance_type, self.frange, self.fstep, self.chunk_size, self.skip_check, self.priority,
    self.parent_id)

    self.setMinimumSize(600, 410)
    self.update_pricing_label()

  @staticmethod
  def _get_divider():
    """Get a divider, a horizontal line used for organizing UI elements."""
    return nuke.Text_Knob('divider', '', '')

  def update_write_dict(self):
    wd = dict()
    for node in (x for x in nuke.allNodes() if x.Class() in WRITE_NODE_CLASSES):
      if not node.knob('disable').value():
        wd[node.name()] = node

    self.writeDict.update(wd)
    self.writeListNames = self.writeDict.keys()
    self.writeListNames.sort()


  @staticmethod
  def _get_caravr_version():
    """Returns CaraVR version, if present or empty string otherwise.

    To discover CaraVR we use plugin path list, which should contain a record of
    the form:
    '/Library/Application Support/Nuke/10.0/plugins/CaraVR/1.0/ToolSets/CaraVR'
    (OSX)
    'C:\\Program Files\\Common
    Files/Nuke/11.0/plugins\\CaraVR\\1.0\\ToolSets/CaraVR' (Windows)
    We take path apart from the right until we encounter "ToolSet" component and
    the one preceding it is
    considered to be a version. This has been offered as a canonical way by The
    Foundry Support.
    """
    cara_plugins = nuke.plugins(nuke.ALL, 'CaraVR')
    for cara_plugin in cara_plugins:
      if 'toolsets' in cara_plugin.lower():
        remaining_path = cara_plugin.lower()
        last_folder_name = ''
        while remaining_path:
          remaining_path, folder_name = os.path.split(remaining_path)
          if last_folder_name == 'toolsets':
            return folder_name
          last_folder_name = folder_name
    return None

  def get_params(self):
    """Returns a dictionary of the job parameters from the submit render gui."""
    params = dict()
    params['plugin_version'] = __version__
    params['num_instances'] = self.num_slots.value()

    for inst_type in self.zync_conn.INSTANCE_TYPES:
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
    params['scene_info'] = {'nuke_version': nuke.NUKE_VERSION_STRING, 'views': nuke.views()}
    caravr_version = ZyncRenderPanel._get_caravr_version()
    if caravr_version:
      params['scene_info']['caravr_version'] = caravr_version

    return params

  def submit_checks(self):
    """Check current settings and raise errors for anything that

    could cause problems when submitting the job.

    Raises:
      zync.ZyncError for any issues found
    """
    if not self.zync_conn.has_user_login():
      raise zync.ZyncError('Please login before submitting a job.')

    if self.existing_project.value().strip() == '' and self.new_project.value().strip() == '':
      raise zync.ZyncError(
          'Project name cannot be blank. Please either choose ' + 'an existing project from the dropdown or enter the desired ' + 'project name in the New Project field.')

    if self.skip_check.value():
      skip_answer = nuke.ask(
          'You\'ve asked Zync to skip the file check ' + 'for this job. If you\'ve added new files to your script this ' + 'job WILL error. Your nuke script will still be uploaded. Are ' + 'you sure you want to continue?')
      if not skip_answer:
        raise zync.ZyncError('Job submission canceled.')

  def submit(self):
    """Does the work to submit the current Nuke script to Zync, given that the parameters on the dialog are set."""

    selected_write_names = []
    selected_write_nodes = []
    for k in self.writeNodes:
      if k.value():
        selected_write_names.append(k.label())
        selected_write_nodes.append(nuke.toNode(k.label()))

    active_viewer = nuke.activeViewer()
    if active_viewer:
      viewer_input = active_viewer.activeInput()
      if viewer_input is None:
        viewed_node = None
      else:
        viewed_node = active_viewer.node().input(viewer_input)
    else:
      viewer_input, viewed_node = None, None

    script_path = nuke.root().knob('name').getValue()
    new_script = self.maybe_correct_path_separators(script_path)
    write_node_to_user_path_map = dict()
    read_dependencies = []

    with WriteChanges(new_script):
      # Nuke 7.0v1 through 7.0v8 broke its own undo() functionality, so this will only run on versions other than those.
      if nuke.NUKE_VERSION_MAJOR != 7 or nuke.NUKE_VERSION_MINOR > 0 or nuke.NUKE_VERSION_RELEASE > 8:
        # Remove all nodes that aren't connected to the Write nodes being rendered.
        select_deps(selected_write_nodes)
        for node in nuke.allNodes():
          if node.isSelected():
            node.setSelected(False)
          else:
            node.setSelected(True)
        nuke.nodeDelete()
        # Freeze expressions on all nodes. Catch errors for Nuke versions that don't support the recurseGroups option.
        try:
          node_list = nuke.allNodes(recurseGroups=True)
        except:
          node_list = nuke.allNodes()
        for node in node_list:
          freeze_node(node)

        _collect_write_node_paths(selected_write_names, write_node_to_user_path_map)
        read_nodes = [read_node for read_node in node_list if read_node.Class() in READ_NODE_CLASSES]
        _collect_read_node_paths(read_nodes, read_dependencies)

    # reconnect the viewer
    if viewer_input is not None and viewed_node is not None:
      nuke.connectViewer(viewer_input, viewed_node)

    # exec before render
    # nuke.callbacks.beforeRenders

    try:
      render_params = self.get_params()
      if render_params is None:
        return
      render_params['scene_info']['write_node_to_output_map'] = write_node_to_user_path_map
      render_params['scene_info']['files'] = read_dependencies
      self.zync_conn.submit_job('nuke', new_script, ','.join(selected_write_names), render_params)
    except zync.ZyncPreflightError as e:
      raise Exception('Preflight Check Failed:\n\n%s' % (str(e),))

    nuke.message('Job submitted to ZYNC.')

  def knobChanged(self, knob):
    """Handles knob callbacks."""
    if knob is self.okButton:
      # Run presubmit checks to make sure the job is ready to be launched with the currently selected parameters. we do
      # this here so we can display errors to the user before the dialog closes and destroys all of their settings. we
      # cannot do the full job submission here though, because trying to use nuke.Undo functionality while a modal
      # dialog is open crashes Nuke.
      try:
        self.submit_checks()
      # Raised exceptions will automatically cause Nuke to abort and leave the dialog open. we just capture that and
      # show a message to the user so they know what went wrong. the full exception will be printed to the Script
      # Editor for further debugging.
      except Exception as e:
        nuke.message(str(e))
        raise
    elif knob is self.loginButton:
      # Run the auth flow, and display the user's email address, adding a little whitespace padding for visual clarity.
      self.userLabel.setValue('  %s' % self.zync_conn.login_with_google())
    elif knob is self.logoutButton:
      self.zync_conn.logout()
      self.userLabel.setValue('')
    elif knob is self.upload_only:
      checked = self.upload_only.value()
      for rk in self.render_knobs:
        rk.setEnabled(not checked)
      for k in self.writeNodes:
        k.setEnabled(not checked)
    elif knob is self.num_slots or knob is self.instance_type:
      self.update_pricing_label()

  def showModalDialog(self):
    """Shows the Zync Submit dialog and does the work to submit it."""
    if nukescripts.panels.PythonPanel.showModalDialog(self):
      self.submit()

  def update_pricing_label(self):
    machine_type = self.instance_type.value().split(' (')[0]
    num_machines = self.num_slots.value()
    machine_type_base = machine_type.split(' ')[-1]
    field_name = 'CP-ZYNC-%s-NUKE' % (machine_type_base.upper(),)
    if 'PREEMPTIBLE' in machine_type.upper():
      field_name += '-PREEMPTIBLE'
    if (field_name in self.zync_conn.PRICING['gcp_price_list'] and 'us' in self.zync_conn.PRICING['gcp_price_list'][
      field_name]):
      cost = '$%.02f' % ((float(num_machines) * self.zync_conn.PRICING['gcp_price_list'][field_name]['us']),)
    else:
      cost = 'Not Available'
    self.pricing_label.setValue('Est. Cost per Hour: %s' % (cost,))

  def maybe_correct_path_separators(self, path):
    if os.sep != '/':
      path = path.replace('/', os.sep)
    path = self.zync_conn.generate_file_path(path)
    if os.sep != '/':
      path = path.replace(os.sep, '/')
    return path


def submit_dialog():
  ZyncRenderPanel().showModalDialog()


def _collect_write_node_paths(selected_write_node_names, write_node_to_user_path_map):
  for write_name in selected_write_node_names:
    write_node = nuke.toNode(write_name)
    if write_node.proxy():
      write_path = write_node.knob('proxy').value()
    else:
      write_path = write_node.knob('file').value()
    output_path, _ = os.path.split(write_path)
    write_node_to_user_path_map[write_name] = output_path


def _collect_read_node_paths(read_nodes, read_node_path_list):
  for read_node in read_nodes:
    read_path = None
    if hasattr(read_node, 'proxy') and read_node.proxy():
      read_path = read_node.knob('proxy').value()
    if not read_path:
      # If proxy is empty, Nuke uses original file path and rescales, so the original file is a dependency to upload
      for knob_name in PATH_KNOB_NAMES:
        if knob_name != 'proxy' and read_node.knob(knob_name):
          read_path = read_node.knob(knob_name).value()
          if read_path:
            break
    if read_path:
      read_node_path_list.append(read_path)
