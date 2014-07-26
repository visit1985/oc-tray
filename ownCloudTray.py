#!/usr/bin/env python
#
#   Copyright (c) 2012 by Michael Goehler <somebody.here@gmx.de>
#
#   This file is part of ownCloudTray.
#
#   ownCloudTray is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   ownCloudTray is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with ownCloudTray.  If not, see <http://www.gnu.org/licenses/>.
#


from gi.repository import Gtk, Gdk, GLib
import pyinotify
import os
import sys
import subprocess
import threading
from optparse import OptionParser
import ConfigParser


class ownCloudTray(pyinotify.ProcessEvent):

    def __init__(self):
        
        self.name = 'ownCloudTray'
        self.version = '0.2.0'
        
        # parse command line arguments
        self.optParser = OptionParser(usage = "usage: %prog [options] filename",
                                      version = "%prog v" + self.version)
        self.optParser.add_option("-d", "--debug",
                                  action="store_true",
                                  dest="debugFlag",
                                  default=False,
                                  help="enable debugging mode")
        (options, args) = self.optParser.parse_args()
        
        # internal status flags
        self.debugFlag = options.debugFlag
        self.firstRun = False
        self.csyncInProgress = False
        self.csyncSubmitAgain = False
        self.csyncForceStop = False
        
        # internal handles
        self.csyncProc = None
        self.csyncThread = None
        self.csyncTimer = None
        
        # create default configuration object
        self.configDefault = ConfigParser.ConfigParser()
        self.configDefault.add_section('csync')
        self.configDefault.set('csync', 'exe',         '/usr/bin/csync')
        self.configDefault.set('csync', 'local_path',  os.environ['HOME'] + '/ownCloud')
        self.configDefault.set('csync', 'protocol',    'owncloud')
        self.configDefault.set('csync', 'user',        '')
        self.configDefault.set('csync', 'password',    '')
        self.configDefault.set('csync', 'host',        'localhost')
        self.configDefault.set('csync', 'port',        '80')
        self.configDefault.set('csync', 'remote_path', '/files/webdav.php')
        self.configDefault.set('csync', 'subfolder',   '')
        self.configDefault.set('csync', 'timeout',     '300')
        
        # read configuration file if exists
        self.configFile = os.path.expanduser('~/.config/' + self.name + '/' + self.name + '.conf')
        self.configFileExample = os.path.expanduser('~/.config/' + self.name + '/' + self.name + '.conf.example')
        self.config = ConfigParser.ConfigParser()
        if self.config.read(self.configFile) == []:
            self.firstRun = True
            configDir = os.path.dirname(self.configFile)
            old_umask = os.umask(077)
            if not os.path.exists(configDir):
                os.makedirs(configDir)
            with open(self.configFileExample, 'wb') as configFileExample:
                self.configDefault.write(configFileExample)
            with open(self.configFile, 'wb') as configFile:
                self.configDefault.write(configFile)
            os.umask(old_umask)
            if self.config.read(self.configFile) == []:
                print "configuration file %s is not accessible" % self.configFile
                sys.exit(1)
        
        # persist configuration to parent object
        self.csyncExe        = self.config.get('csync', 'exe')
        self.csyncLocalPath  = self.config.get('csync', 'local_path')
        self.csyncProtocol   = self.config.get('csync', 'protocol')
        self.csyncUser       = self.config.get('csync', 'user')
        self.csyncPassword   = self.config.get('csync', 'password')
        self.csyncHost       = self.config.get('csync', 'host')
        self.csyncPort       = self.config.getint('csync', 'port')
        self.csyncRemotePath = self.config.get('csync', 'remote_path')
        self.csyncSubfolder  = self.config.get('csync', 'subfolder')
        self.csyncTimeout    = self.config.getint('csync', 'timeout')
        
        # load ui from glade file
        self.uifile = os.path.join(os.path.dirname(__file__), 'gui/ownCloudTray.glade')
        
        # create tray icon
        self.statusIconInactive = Gtk.Image.new_from_file(os.path.join(os.path.dirname(__file__), 'img/owncloud_inactive.svg'))
        self.statusIconSyncing = Gtk.Image.new_from_file(os.path.join(os.path.dirname(__file__), 'img/owncloud_syncing.svg'))
        self.statusIconError = Gtk.Image.new_from_file(os.path.join(os.path.dirname(__file__), 'img/owncloud_error.svg'))
        self.statusIcon = Gtk.StatusIcon()
        self.statusIcon.set_from_pixbuf(self.statusIconInactive.get_pixbuf())
        self.statusIcon.set_title(self.name)
        self.statusIcon.set_tooltip_text(self.name + ' ' + self.version)
        
        # create popup menu
        self.menu = Gtk.Menu()
        self.menuItem = Gtk.ImageMenuItem.new_from_stock(Gtk.STOCK_REFRESH, None)
        self.menuItem.connect('activate', self.cbForceSync, self.statusIcon)
        self.menu.append(self.menuItem)
        self.menuItem = Gtk.ImageMenuItem.new_from_stock(Gtk.STOCK_PROPERTIES, None)
        self.menuItem.connect('activate', self.cbProperties, self.statusIcon)
        self.menu.append(self.menuItem)
        self.menuItem = Gtk.ImageMenuItem.new_from_stock(Gtk.STOCK_ABOUT, None)
        self.menuItem.connect('activate', self.cbAbout, self.statusIcon)
        self.menu.append(self.menuItem)
        self.menuItem = Gtk.ImageMenuItem.new_from_stock(Gtk.STOCK_QUIT, None)
        self.menuItem.connect('activate', self.cbQuit, self.statusIcon)
        self.menu.append(self.menuItem)
        
        # connect popup menu to tray icon
        self.statusIcon.connect('popup-menu', self.cbPopupMenu, self.menu)
        
        # show tray icon
        self.statusIcon.set_visible(True)
        
        # initialize notifier
        self.watchman = pyinotify.WatchManager()
        self.watchdesc = None
        self.mask = pyinotify.IN_DELETE | pyinotify.IN_CREATE | pyinotify.IN_MODIFY | pyinotify.IN_MOVED_FROM | pyinotify.IN_MOVED_TO
        self.notifier = pyinotify.ThreadedNotifier(self.watchman, self)
        self.notifier.start()
        
    
    # main method
    def main(self):
        
        # if first run start properties dialog
        if self.firstRun == True:
            self.cbProperties(None, None, None)
        
        # if directory exists
        if os.path.isdir(self.csyncLocalPath):
            
            # remove previously added directory from notifier
            if not self.watchdesc == None:
                self.unwatch()
                
            # add new directory to notifier
            self.watch(self.csyncLocalPath)
        
        # enable threading
        GLib.threads_init()
        Gdk.threads_init()
        Gdk.threads_enter()
        
        # sync on start
        self.cbSync()
        
        # enter main loop
        try:
            Gtk.main()
        except KeyboardInterrupt:
            Gtk.main_quit()
            
            #--
            Gdk.threads_leave()
            self.csyncTimer.cancel()
            self.notifier.stop()
        
        # clean exit
        Gdk.threads_leave()
        self.csyncTimer.cancel()
        self.notifier.stop()


    # add new directory to notifier
    def watch(self, directory):
        self.watchdesc = self.watchman.add_watch(directory, self.mask, rec=True)
    
    
    # remove previously added directory from notifier
    def unwatch(self):
        self.watchman.rm_watch(self.watchdesc.values(), rec=True)
        
        
    # child process to spawn 
    def newThread(self, cbExit, args):
        
        if self.debugFlag == True:
            self.csyncProc = subprocess.Popen(args)
            self.csyncProc.wait()
        else:
            devnull = open(os.devnull, 'w')
            self.csyncProc = subprocess.Popen(args, stdout = devnull)
            self.csyncProc.wait()
            devnull.close()
        
        cbExit(self.csyncProc.returncode)


    # callback of the child process
    def cbThread(self, returncode):
        
        if returncode == 0:
            #self.statusIcon.set_from_stock(Gtk.STOCK_YES)
            self.statusIcon.set_from_pixbuf(self.statusIconInactive.get_pixbuf())
            self.statusIcon.set_tooltip_text(self.name + ' ' + self.version)
        else:
            #self.statusIcon.set_from_stock(Gtk.STOCK_CANCEL)
            self.statusIcon.set_from_pixbuf(self.statusIconError.get_pixbuf())
            self.statusIcon.set_tooltip_text(self.name + ': error in synchronization')
            
        print 'End %s with returncode %s' % (self.csyncExe ,returncode)
        
        if not self.csyncForceStop == True:
            self.csyncInProgress = False

            if self.csyncSubmitAgain == True:
                self.cbSync()
            else:
                self.csyncTimer = threading.Timer(self.csyncTimeout, self.cbSync)
                self.csyncTimer.start()
        else:
            self.csyncTimer.cancel()


    # main synchronization method
    def cbSync(self):
        
        if self.csyncInProgress == False:
            self.csyncSubmitAgain = False
            self.csyncInProgress = True
        
            # change the status icon
            #self.statusIcon.set_from_stock(Gtk.STOCK_REFRESH)
            self.statusIcon.set_from_pixbuf(self.statusIconSyncing.get_pixbuf())
            self.statusIcon.set_tooltip_text(self.name + ': synchronization in progress')
            
            # create the csync command
            csyncArgs = [self.csyncExe, self.csyncLocalPath, self.csyncProtocol + '://' + self.csyncUser + ':' + self.csyncPassword + '@' + self.csyncHost + ':' + str(self.csyncPort) + self.csyncRemotePath + '/' + self.csyncSubfolder]
            
            # start sub-thread
            self.csyncThread = threading.Thread(target=self.newThread, args=(self.cbThread, csyncArgs))
            self.csyncThread.start()
            
            print 'Started %s' % self.csyncExe
        
        else:
            self.csyncSubmitAgain = True
            
            print 'Scheduled %s' % self.csyncExe


    # start synchronization immediately
    def cbForceSync(self, widget, event, data = None):
        if not self.csyncTimer == None:
            self.csyncTimer.cancel()
            
        self.cbSync()
        
        self.csyncTimer = threading.Timer(self.csyncTimeout, self.cbSync)
        self.csyncTimer.start()
        
    
    # about dialog
    def cbAbout(self, widget, event, data = None):
        window = Gtk.AboutDialog()
        window.set_destroy_with_parent(True)
        window.set_program_name(self.name)
        window.set_version(self.version)
        window.set_copyright('Copyright (c) 2012 by Michael Goehler')
        window.set_authors(['Michael Goehler'])
        window.set_website('http://blog.myjm.de/')
        window.set_website_label('http://blog.myjm.de/')
        window.set_logo(self.statusIconInactive.get_pixbuf())
        window.run()
        window.destroy()
        
    
    # properties dialog
    def cbProperties(self, widget, event, data = None):
        builder = Gtk.Builder()
        builder.add_from_file(self.uifile)
        window = builder.get_object ('dialogProperties')
        
        buttonExe = builder.get_object('buttonExe')
        buttonExe.set_filename(self.csyncExe)
        
        buttonLocalPath = builder.get_object('buttonLocalPath')
        buttonLocalPath.set_filename(self.csyncLocalPath)
        
        buttonProtocol = builder.get_object('buttonProtocol')
        buttonProtocolModel = buttonProtocol.get_model()
        for item in buttonProtocolModel:
            if item[0] == self.csyncProtocol:
                buttonProtocol.set_active_iter(item.iter)
                break
        
        entryUser = builder.get_object('entryUser')
        entryUser.set_text(self.csyncUser)
        
        entryPassword = builder.get_object('entryPassword')
        entryPassword.set_text(self.csyncPassword)
        
        entryHost = builder.get_object('entryHost')
        entryHost.set_text(self.csyncHost)
        
        entryPort = builder.get_object('entryPort')
        entryPort.set_text(str(self.csyncPort))
        
        entryRemotePath = builder.get_object('entryRemotePath')
        entryRemotePath.set_text(self.csyncRemotePath)
        
        entrySubfolder = builder.get_object('entrySubfolder')
        entrySubfolder.set_text(self.csyncSubfolder)
        
        buttonTimeout = builder.get_object('buttonTimeout')
        buttonTimeout.set_value(self.csyncTimeout)
        
        # save button pressed
        if window.run() == Gtk.ResponseType.OK:
            
            self.csyncExe = buttonExe.get_filename()
            self.csyncLocalPath = buttonLocalPath.get_filename()
            item = buttonProtocol.get_active()
            self.csyncProtocol = buttonProtocolModel[item][0]
            self.csyncUser = entryUser.get_text()
            self.csyncPassword = entryPassword.get_text()
            self.csyncHost = entryHost.get_text()
            self.csyncPort = entryPort.get_text()
            self.csyncRemotePath = entryRemotePath.get_text()
            self.csyncSubfolder = entrySubfolder.get_text()
            self.csyncTimeout = buttonTimeout.get_value_as_int()
            
            if not os.access(self.csyncExe, os.X_OK):
                print '%s is not an executable' % self.csyncExe
            
            if not self.watchdesc == None:
                self.unwatch()
            
            if os.path.isdir(self.csyncLocalPath):
                self.watch(self.csyncLocalPath)
            else:
                print '%s is not a directory' % self.csyncLocalPath
            
            if not self.csyncTimer == None:
                self.csyncTimer.cancel()
            
            self.csyncTimer = threading.Timer(self.csyncTimeout, self.cbSync)
            self.csyncTimer.start()
            
            self.config.set('csync', 'exe',         self.csyncExe)
            self.config.set('csync', 'local_path',  self.csyncLocalPath)
            self.config.set('csync', 'protocol',    self.csyncProtocol)
            self.config.set('csync', 'user',        self.csyncUser)
            self.config.set('csync', 'password',    self.csyncPassword)
            self.config.set('csync', 'host',        self.csyncHost)
            self.config.set('csync', 'port',        self.csyncPort)
            self.config.set('csync', 'remote_path', self.csyncRemotePath)
            self.config.set('csync', 'subfolder',   self.csyncSubfolder)
            self.config.set('csync', 'timeout',     self.csyncTimeout)
            with open(self.configFile, 'wb') as configFile:
                self.config.write(configFile)
                
            self.cbSync()
        
        # cancel button pressed
        #elif window.run() == Gtk.ResponseType.CANCEL:

        # unexpected close of dialog
        #else:
        
        window.destroy()
    
       
    # quit application
    def cbQuit(self, widget, data = None):
        self.csyncForceStop = True
        Gtk.main_quit()


    # popup menu on status icon right click
    def cbPopupMenu(self, widget, button, time, data = None):
        if button == 3:
            if data:
                data.show_all()
                data.popup(None, None, self.pos, self.statusIcon, button, time)


    # find position for menu
    def pos(self, menu, icon):
                return (Gtk.StatusIcon.position_menu(menu, icon))
    
    
    def process_IN_CREATE(self, event):
        if event.name != '.csync_timediff.ctmp':
            print 'Sync triggered by creation of %s' % os.path.join(event.path, event.name)
            self.cbSync()
        
        
    def process_IN_DELETE(self, event):
        if event.name != '.csync_timediff.ctmp':
            print 'Sync triggered by deletion of %s' % os.path.join(event.path, event.name)
            self.cbSync()
        
        
    def process_IN_MODIFY(self, event):
        if event.name != '.csync_timediff.ctmp':
            print 'Sync triggered by modifing %s' % os.path.join(event.path, event.name)
            self.cbSync()
        
        
    def process_IN_MOVED_FROM(self, event):
        if event.name != '.csync_timediff.ctmp':
            print 'Sync triggered by moving out %s' % os.path.join(event.path, event.name)
            self.cbSync()
    
    
    def process_IN_MOVED_TO(self, event):
        if event.name != '.csync_timediff.ctmp':
            print 'Sync triggered by moving in %s' % os.path.join(event.path, event.name)
            self.cbSync()
        
    
if __name__ == '__main__':
    ownCloudTray = ownCloudTray()
    ownCloudTray.main()

