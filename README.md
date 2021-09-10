# Zync Plugin for Nuke

**Notice:** this project has been archived and is no longer being maintained.

Tested with all versions of Nuke 7 and 8.

## zync-python

This plugin depends on zync-python, the Zync Python API.

Before trying to install zync-nuke, make sure to [download zync-python](https://github.com/zync/zync-python) and follow the setup instructions there.

# Warning

Note that the simplest and recommended way to install Zync plugins is through the  Zync Client Application  (see [instructions](https://docs.zyncrender.com/install-and-setup#option-1-the-plugins-tab-in-the-zync-client-app-simple-recommended-for-most-users)). The steps described below are for advanced users and we recommend to proceed with them only if you need to modify the plugin code for your custom needs.

## Clone the Repository

Clone this repository to the desired location on your local system. If you're doing a site-wide plugin install, this will have to be a location accessible by everyone using the plugins.

## Config File

Contained in this folder you'll find a file called ```config_nuke.py.example```. Make a copy of this file in the same directory, and rename it ```config_nuke.py```.

Edit ```config_nuke.py``` in a Text Editor. It defines one config variable - `API_DIR` - the full path to your zync-python directory.

Set `API_DIR` to point to the zync-python you installed earlier, save the file, and close it.

## Set Up menu.py

You'll need to locate your .nuke folder, usually stored within your HOME folder. In there you'll need a file called menu.py. This file may already exist.

menu.py should contain the following text:

```python
import nuke
nuke.pluginAddPath('/path/to/zync-nuke')
import zync_nuke
menubar = nuke.menu('Nuke');
menu = menubar.addMenu('&Render')
menu.addCommand('Render on Zync', 'zync_nuke.submit_dialog()')
```

This will add an item to the "Render" menu in Zync that will allow you to launch Zync jobs.

## Done

That's it! Restart Nuke to pull in the changes you made.
