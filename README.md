# Zync Plugin for Nuke

Tested with all versions of Nuke 7 and 8. 

## zync-python

This plugin depends on zync-python, the Zync Python API.

Before trying to install zync-nuke, make sure to [download zync-python](https://github.com/zync/zync-python) and follow the setup instructions there.

## Clone the Repository

Clone this repository to the desired location on your local system. If you're doing a site-wide plugin install, this will have to be a location accessible by everyone using the plugins. 

## Register Script

Log in to the Zync Web Console, and go to the My Account page.

In the "Scripts" section, you may see a script called "nuke_plugin". If you do, save the API key shown there for the next step. If you don't see "nuke_plugin", create a new script with that name. An API key will be generated which you'll use in the next step.

## Config File

Contained in this folder you'll find a file called ```config_nuke.py.example```. Make a copy of this file in the same directory, and rename it ```config_nuke.py```.

Edit ```config_nuke.py``` in a Text Editor. It defines two config variables:

```API_DIR``` - the full path to your zync-python directory.

```API_KEY``` - the API Key of the registered script, from the previous step.

Set these variables, save the file, and close it.

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

