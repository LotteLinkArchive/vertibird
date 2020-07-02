#!/usr/bin/env python
"""
Vertibird is a dead-simple virtualization library based around direct access to
QEMU. Because I couldn't be bothered to figure out how the hell the crappy
libvirt C bindings work. I also couldn't be bothered to write a million lines
of XML. Screw that.
"""

import shelve, threading, time, uuid, socket, subprocess, os, telnetlib, signal
import sys, shlex, random, string, psutil, zlib, builtins, select, tempfile, io

from contextlib import closing

from vncdotool import api as vncapi
from PIL import Image, ImageDraw
from filelock import Timeout, FileLock, SoftFileLock

from sqlalchemy import create_engine
from sqlalchemy import Column, Integer, String, PickleType, Boolean
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import scoped_session
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.declarative import declarative_base

__author__ = 'Naphtha Nepanthez'
__version__ = '0.0.1'
__license__ = 'MIT' # SEE LICENSE FILE

GLOBAL_LOOPBACK = '127.0.0.1'
QEMU_VNC_ADDS = 5900
TELNET_TIMEOUT_SECS = 1
VNC_TIMEOUT_SECS = TELNET_TIMEOUT_SECS
STATE_CHECK_CLK_SECS = 0.03
AUDIO_CLEAR_INTERVAL = 1
AUDIO_MAX_SIZE = 4194304
AUDIO_BLOCK_SIZE = 8192
AUDIO_CHUNKS = 1024
DEFAULT_DSIZE = 8589934592
VNC_IMAGE_MODE = 'RGB'
DISK_FORMAT = 'raw'
DEBUG = False
BLANK_WAV_HEADER =\
    b'RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01'\
    b'\x00\x02\x00D\xac\x00\x00\x10\xb1\x02\x00\x04\x00'\
    b'\x10\x00data\x00\x00\x00\x00'

class Vertibird(object):
    """
    WARNING: Be careful with what kinds of input you feed Vertibird. Everything
    will probably end up being fed to the commandline in some way or another
    due to the nature of QEMU. If you want my advice, you should ensure that
    device names, file paths, etc are kept short, restricted to alphanumeric
    characters only, and absolutely no characters such as the comma, space,
    semicolon, etc.
    
    Just ALWAYS remember that anything you input will be converted into
    commandline arguments for QEMU.
    """
    
    class IncompatibleOperatingSystem(Exception):
        pass
    
    Base = declarative_base()

    def __init__(
                self,
                qemu: str = 'qemu-kvm',
                persistence: str = 'sqlite:///vertibird.db'
            ):
        """
        Create a Vertibird hypervisor interface.
        
        Parameters
        ----------
        qemu :
            The QEMU executable to use, defaults to qemu-kvm
        persistence :
            The shelf/database to store information about created VMs in,
            defaults to sqlite:///vertibird.db
        
        Raises
        ------
        Vertibird.IncompatibleOperatingSystem
            Raised when your OS doesn't support the stuff Vertibird needs to
            work.
        """
        
        # Not sure if this is a good idea
        if not ('linux' in sys.platform.lower()):
            raise IncompatibleOperatingSystem('Only Linux is supported.')
        
        self.qemu = qemu
        self.engine = create_engine(persistence, strategy='threadlocal')
        self.Base.metadata.create_all(self.engine)
        self.db = scoped_session((sessionmaker(bind = self.engine)))()
    
    def create(self):
        """
        Creates a virtual machine with a random UUID and returns the live
        access object.
        """
        x = self.VertiVM(id = str(uuid.uuid4()), ports = self._new_ports())
        self.db.add(x)
        self.db.commit()
        
        return self.__wrap_live(x)
        
    def get(self, vmuuid = str):
        """
        Retrieves a Vertibird.VertiVMLive object via the given UUID.
        """
        return self.__wrap_live(self.db.query(self.VertiVM).filter(
            self.VertiVM.id == vmuuid
        ).one())
    
    def remove(self, vmuuid = str):
        """
        Removes a virtual machine of given UUID.
        """
        self.get(vmuuid).remove()
        
    def create_drive(self, img: str, size: int = DEFAULT_DSIZE):
        """
        Creates a virtual machine drive at the specified path and of the
        specified size. Size is in bytes and defaults to DEFAULT_DSIZE.
        
        Virtual machine drives are created as sparse files, so ensure that your
        filesystem supports them - it's an exceedingly useful feature, and
        contributes to the performance of Vertibird significantly!
        """
        if not os.path.isfile(img):
            f = open(img, 'wb')
            f.truncate(size)
            f.close()
        else:
            raise self.DriveAlreadyExists(img)
        
    def list(self):
        """
        Returns a list of all the available virtual machine UUIDs.
        """
        
        return list(
            map(
                (lambda x: x[0]),
                self.db.query(self.VertiVM.id).all()
            )
        )
        
    def __wrap_live(self, db_object):
        return self.VertiVMLive(self, self.db, db_object)
    
    class DriveAlreadyExists(Exception):
        pass
    
    class VertiVMLive(object):
        class InvalidStateChange(Exception):
            pass
            
        class LaunchDependencyMissing(Exception):
            pass
        
        class InvalidDriveType(Exception):
            pass
            
        class InvalidGenericDeviceType(Exception):
            pass
            
        class VMLaunchException(Exception):
            pass
            
        class VMDisplay(object):
            __shared_audio = {}
            
            def disconnect(self):
                if self.client != None:
                    del self.client
                
                self.paste = self.__return_none
                self.mouseMove = self.__return_none
                self.mouseDown = self.__return_none
                self.mouseUp = self.__return_none
                self.keyDown = self.__return_none
                self.keyUp = self.__return_none
                
                self.client = None
            
            def refresh(self):
                """
                Refresh the display
                """
                
                try:
                    self.client.refreshScreen()
                except (TimeoutError, AttributeError, builtins.AttributeError):
                    self.disconnect()
            
            def capture(self):
                """
                Returns a PIL image of the virtual machine display. You can
                also interact with the VM through the following functions:
                    - paste()
                    - mouseMove()
                    - mouseDown()
                    - mouseUp()
                    - keyDown()
                    - keyUp()
                For an explanation of how they work, see the following...
                    - https://vncdotool.readthedocs.io/en/latest/modules.html
                    - https://vncdotool.readthedocs.io/en/latest/library.html
                """
                
                self.refresh()
                
                if self.client != None:
                    self.shape = self.client.screen.size
                    
                    return self.client.screen.convert(
                        VNC_IMAGE_MODE,
                        dither  = Image.NONE,
                        palette = Image.ADAPTIVE
                    )
                else:
                    offline_message = Image.new(VNC_IMAGE_MODE, self.shape)
                    ImageDraw.Draw(
                        offline_message
                    ).text(
                        (8, 8),
                        'NO SIGNAL',
                        (255, 255, 0)
                    )
                    
                    self.connect()
                    
                    return offline_message
            
            def connect(self):
                self.disconnect()
                start_time = time.time()
                
                while (self.client == None
                        or (time.time() - start_time) > VNC_TIMEOUT_SECS
                    ) and (self.vmlive.state() == 'online'):
                        
                    try:
                        self.client = vncapi.connect('{0}:{1}'.format(
                            GLOBAL_LOOPBACK,
                            (self.vmlive.db_object.ports['vnc']
                            - QEMU_VNC_ADDS)
                        ), password = None, timeout = VNC_TIMEOUT_SECS)
                        
                        self.paste = self.client.paste
                        self.mouseMove = self.client.mouseMove
                        self.mouseDown = self.client.mouseDown
                        self.mouseUp = self.client.mouseUp
                        self.keyDown = self.client.keyDown
                        self.keyUp = self.client.keyUp
                        
                        self.capture()
                    except (
                            vncapi.VNCDoException,
                            TimeoutError,
                            AttributeError,
                            builtins.AttributeError
                        ):
                        self.disconnect()
            
            def __return_none(self, *args, **kwargs):
                return None
            
            def audio_grab(self):
                """
                Get the virtual machine's current audio buffer. Returns a wav
                audio stream in bytes. Might not contain any headers.
                """
                
                if self.vmlive.audiopipe != None:
                    try:
                        ret = bytearray(self.__shared_audio[self.vmlive.id])
                        self.__shared_audio[self.vmlive.id].clear()
                    except KeyError:
                        ret = bytearray()
                else:
                    ret = bytearray()
                    
                if (ret[:4] == b'RIFF'
                    and ret[8:12] == b'WAVE'):
                    
                    ret = ret[44:]
                    
                blank = bytearray(BLANK_WAV_HEADER)
                
                blank[4:8] = (
                    (len(blank) + len(ret)) - 8
                ).to_bytes(4, byteorder='little')
                
                blank[40:44] = (
                    len(ret)
                ).to_bytes(4, byteorder='little')
                
                ret = blank + ret
                    
                return ret
            
            
            def __audio_thread(self):
                self.__shared_audio[self.vmlive.id] = bytearray()
                
                while True:
                    if self.vmlive.audiopipe != None:
                        try:
                            # This lock is important, as it prevents
                            # multiple processes screwing around with the
                            # named pipe. You only need one.
                            with FileLock(self.vmlive.audiopipe):
                                f = open(self.vmlive.audiopipe, 'rb')
                                
                                # # Experimenting with os.read() instead...
                                # p = f.read(AUDIO_BLOCK_SIZE)
                                p = os.read(f.fileno(), AUDIO_BLOCK_SIZE)
                                
                                self.__shared_audio[self.vmlive.id] += p
                                
                                if len(
                                        self.__shared_audio[self.vmlive.id]
                                    ) > AUDIO_MAX_SIZE:
                                        
                                    self.__shared_audio[self.vmlive.id].clear()
                        except FileNotFoundError:
                            self.vmlive.audiopipe = None
                    else:
                        time.sleep(STATE_CHECK_CLK_SECS)
            
            def __init__(self, vmlive):
                self.vmlive = vmlive
                self.client = None
                self.disconnect()
                self.shape = (640, 480)
                
                self.threads = []
                self.threads.append(
                    threading.Thread(
                        target = self.__audio_thread,
                        daemon = True
                    )
                )
                self.threads[-1].start()
                
                self.connect()
                        
        def __init__(self, vertibird, db_session, db_object):
            """
            Don't call this directly, but know that it assigns the VMDisplay
            object to self.display. This will allow you to interact with the VM
            """
            
            self.vertibird  = vertibird
            self.db_session = db_session
            self.db_object  = db_object
            self.audiopipe  = None
            self.id         = db_object.id
            self.display    = self.VMDisplay(self)
            
            # State checking stuff
            self._state_check()
            
        def wait(self):
            """
            Waits until this VM has been terminated.
            """
            
            while self.state() != 'offline':
                time.sleep(STATE_CHECK_CLK_SECS)
            
        def remove(self):
            """
            Removes the virtual machine from the database. Will NOT delete
            the storage or mounted ISOs. It is recommended to call
            Vertibird.remove() instead.
            """
            
            try:
                self.stop()
            except self.InvalidStateChange:
                pass # Already offline
            
            self.db_session.delete(self.db_object)
            self.db_session.commit()
            
        def start(self):
            """
            Starts the virtual machine's QEMU process. Will raise an exception
            if the virtual machine is already running.
            """
            
            if self.state() == 'offline':
                self.__randomize_ports()
                
                self.db_object.audiopipe = tempfile.mkstemp(suffix='.wav')[1]
                self.db_session.commit()
                os.remove(self.db_object.audiopipe)
                os.mkfifo(self.db_object.audiopipe)
                self.audiopipe = self.db_object.audiopipe
                
                arguments = [
                    self.vertibird.qemu, # PROCESS
                    '-monitor',
                    'telnet:{0}:{1},server,nowait'.format(
                        GLOBAL_LOOPBACK,
                        self.db_object.ports['monitor']
                    ),
                    '-nographic',
                    '-serial',
                    'none',
                    '-vnc',
                    '{0}:{1},share=force-shared'.format(
                        GLOBAL_LOOPBACK,
                        self.db_object.ports['vnc'] - QEMU_VNC_ADDS
                    ),
                    '-m',
                    '{0}B'.format(self.db_object.memory),
                    '-boot',
                    'order={0},menu=on'.format(
                        shlex.quote(self.db_object.bootorder)
                    ),
                    '-cpu',
                    shlex.quote(self.db_object.cpu),
                    '-smp',
                    str(self.db_object.cores),
                    '-machine',
                    shlex.quote(self.db_object.machine),
                    '-object',
                    'rng-random,id=rng0,filename=/dev/urandom',
                    '-device',
                    'virtio-rng-pci,rng=rng0',
                    '-vga',
                    shlex.quote(self.db_object.vga),
                    '-device',
                    'lsi53c895a,id=scsi',
                    '-device',
                    'ahci,id=ahci',
                    '-audiodev',
                    'wav,path={0},id=audioout'.format(
                        shlex.quote(self.db_object.audiopipe)
                    ),
                    '-device',
                    'rtl8139,netdev=net0',
                    '-netdev',
                    'user,id=net0{0}{1}'.format(
                        (lambda x: ',' if x > 0 else '')(
                            len(self.db_object.forwarding)
                        ),
                        ','.join([
                            'hostfwd={0}:{1}:{2}-:{3}'.format(
                                shlex.quote(x['protocol']),
                                shlex.quote(x['external_ip']),
                                shlex.quote(x['external_port']),
                                shlex.quote(x['internal_port'])
                            ) for x in self.db_object.forwarding
                        ])
                    )
                ]
                
                if self.db_object.sound == 'ac97':
                    arguments += [
                        '-device',
                        'AC97,audiodev=audioout'
                    ]
                elif self.db_object.sound == 'hda':
                    arguments += [
                        '-device',
                        'intel-hda,id=hda',
                        '-device',
                        'hda-output,id=hda-codec,audiodev=audioout'
                    ]
                else:
                    raise InvalidGenericDeviceType(
                        'Audio device type must be either ac97 or hda.'
                    )
                
                strdevices = 0
                
                for key, cdrom in enumerate(self.db_object.cdroms):
                    if os.path.isfile(cdrom):
                        internal_id = self.__random_device_id()
                        
                        arguments += [
                            '-drive',
                            'id={0},file={1},if=none,media=cdrom'.format(
                                internal_id,
                                shlex.quote(os.path.abspath(cdrom))
                            ),
                            '-device',
                            'ide-cd,drive={0},bus=ide.0,unit={1}'.format(
                                internal_id,
                                strdevices
                            )
                        ]
                        strdevices += 1
                    else:
                        raise LaunchDependencyMissing(cdrom)
                        
                for key, drive in enumerate(self.db_object.drives):
                    internal_id = self.__random_device_id()
                    
                    if os.path.isfile(drive['path']):
                        if drive['type'] in ['ahci', 'ide']:
                            arguments += [
                                '-drive',
                                'id={0},file={1},if=none,format={2}'.format(
                                    internal_id,
                                    shlex.quote(
                                        os.path.abspath(drive['path'])
                                    ),
                                    DISK_FORMAT
                                ),
                                '-device',
                                'ide-hd,drive={0},bus={1}.0,unit={2}'.format(
                                    internal_id,
                                    (lambda x:
                                        'ahci' if x == 'ahci' else 'ide'
                                    )(drive['type']),
                                    strdevices
                                )
                            ]
                            strdevices += 1
                        elif drive['type'] == 'scsi':
                            arguments += [
                                '-drive',
                                'id={0},file={1},if=none,format={2}'.format(
                                    internal_id,
                                    shlex.quote(
                                        os.path.abspath(drive['path'])
                                    ),
                                    DISK_FORMAT
                                ),
                                '-device',
                                'scsi-hd,drive={0},bus=scsi.0,unit={1}'.format(
                                    internal_id,
                                    strdevices
                                )
                            ]
                            strdevices += 1
                        elif drive['type'] == 'virtio':
                            arguments += [
                                '-drive',
                                'id={0},file={1},if=virtio,format={2}'.format(
                                    internal_id,
                                    shlex.quote(
                                        os.path.abspath(drive['path'])
                                    ),
                                    DISK_FORMAT
                                )
                            ]
                        else:
                            raise InvalidGenericDeviceType(
                                'Drive type must be virtio, scsi, ahci or ide.'
                            )
                    else:
                        raise LaunchDependencyMissing(drive['path'])
                
                if DEBUG:
                    arguments.remove('-nographic')
                    arguments += [
                        '-display',
                        'gtk'
                    ]
                
                # VM LAUNCH
                process = subprocess.Popen(
                    arguments,
                    stderr = subprocess.PIPE,
                    stdout = subprocess.PIPE
                )
                pid = process.pid
                
                self.db_object.handles = (
                    process.stderr.fileno(),
                    process.stdout.fileno()
                )
                self.db_object.pid   = pid
                self.db_object.state = 'online'
                self.db_session.commit()
                
                self.display.connect()
            else:
                raise self.InvalidStateChange('Invalid state for start()!')
        
        def get_properties(self):
            """
            Returns a dictionary containing the properties for the virtual
            machine, such as the memory, core count, CPU model, machine model
            and graphics adapter model.
            """
            return {
                'memory'    : self.db_object.memory   ,
                'cores'     : self.db_object.cores    ,
                'cpu'       : self.db_object.cpu      ,
                'machine'   : self.db_object.machine  ,
                'vga'       : self.db_object.vga      ,
                'sound'     : self.db_object.sound    ,
                'bootorder' : self.db_object.bootorder,
            }
        
        def set_properties(self, properties: dict):
            """
            Replaces all of the virtual machine options with the contents of a
            property dictionary, of which can be obtained with get_properties()
            """
            self.__set_option_offline()
            
            self.db_object.memory    = int(properties['memory'   ])
            self.db_object.cores     = int(properties['cores'    ])
            self.db_object.cpu       = str(properties['cpu'      ])
            self.db_object.machine   = str(properties['machine'  ])
            self.db_object.vga       = str(properties['vga'      ])
            self.db_object.sound     = str(properties['sound'    ])
            self.db_object.bootorder = str(properties['bootorder'])
            self.db_session.commit()
        
        def forward_port(self,
                external_port,
                internal_port,
                protocol: str = 'tcp',
                external_ip: str = '0.0.0.0'
            ) -> str:
            """
            Forwards a port from within the VM to a port outside of it.
            Returns the forwarding ID.
            """
            
            self.__set_option_offline()
            
            fwd_id = str(zlib.crc32(('-'.join([
                protocol,
                external_ip,
                str(external_port),
                str(internal_port)
            ])).encode()))
            
            if not (fwd_id in list(map(
                    (lambda x: x['id']),
                    self.db_object.forwarding
                ))):
                    
                self.db_object.forwarding = (self.db_object.forwarding + [
                    {
                        'id': fwd_id,
                        'protocol': protocol,
                        'external_ip': external_ip,
                        'external_port': str(external_port),
                        'internal_port': str(internal_port)
                    },
                ])
                self.db_session.commit()
            
            return fwd_id
            
        def list_forwardings(self):
            """
            Returns a list containing dictionaries, all of which are different
            port forwards specified for this VM.
            """
            
            return self.db_object.forwarding
            
        def remove_forwarding(self, fwd_id: str):
            """
            Remove a port forward based on forward ID.
            """
            
            self.__set_option_offline()
            
            self.db_object.forwarding = list(filter(
                lambda x: x['id'] != fwd_id,
                self.db_object.forwarding
            ))
            self.db_session.commit()
        
        def attach_cdrom(self, iso: str):
            """
            Attaches a path to a CD-ROM iso to the virtual machine.
            """
            
            self.__set_option_offline()
            
            if not os.path.isfile(iso):
                raise LaunchDependencyMissing(iso)
            
            if not (iso in self.db_object.cdroms):
                # Weird appending is required to trigger dirty state
                self.db_object.cdroms = (self.db_object.cdroms + [iso,])
                self.db_session.commit()
                
        def list_cdroms(self):
            """
            Returns a list containing strings, all of which are paths to the
            ISOs attached to the VM as CD-ROM drives.
            """
            
            return self.db_object.cdroms
            
        def detach_cdrom(self, iso: str):
            """
            Detaches a given CD-ROM iso path from the virtual machine.
            """
            
            self.__set_option_offline()
            
            if (iso in self.db_object.cdroms):
                self.db_object.cdroms = list(filter(
                    lambda x: x != iso,
                    self.db_object.cdroms
                ))
                self.db_session.commit()
                
        def attach_drive(self, img: str, dtype: str = 'ide'):
            """
            Attaches a drive to the virtual machine.
            
            Parameters
            ----------
            img :
                The relative OR absolute path of the image. This will be used
                to identify the image.
            dtype :
                The interface type for the drive.
                Can be IDE, AHCI, SCSI or VirtIO.
            """
            
            self.__set_option_offline()
            
            dtype = dtype.lower()
            if not (dtype in ['ide', 'scsi', 'virtio', 'ahci']):
                raise self.InvalidDriveType('No such type {0}.'.format(dtype))
                
            if not os.path.isfile(img):
                raise LaunchDependencyMissing(img)
                
            if not (img in list(map(
                    lambda x: x['path'],
                    self.db_object.drives
                ))):
                    
                self.db_object.drives = self.db_object.drives + [{
                    'path': img,
                    'type': dtype
                },]
                
                self.db_session.commit()
            
        def list_drives(self):
            """
            Returns a list of dictionaries specifying the path and interface
            type of each drive attached to the virtual machine.
            """
            
            return self.db_object.drives
            
        def detach_drive(self, img: str):
            """
            Detaches a given drive path from the VM.
            """
            
            self.__set_option_offline()
            
            self.db_object.drives = list(filter(
                lambda x: x['path'] != img,
                self.db_object.drives
            ))
            self.db_session.commit()
            
        def create_or_attach_drive(
                self, img: str, size: int = DEFAULT_DSIZE, dtype: str = 'ide'
            ):
            """
            Create a drive image if it doesn't exist and attach it if it isn't
            already attached. See Vertibird.create_drive() and
            Vertibird.VertiVMLive.attach_drive() for argument explanations.
            """
                
            self.__set_option_offline()
            
            if not os.path.isfile(img):
                self.vertibird.create_drive(img, size)
            else:
                self.attach_drive(img, dtype)
        
        def signal_shutdown(self):
            """
            Sends the shutdown signal. This is non-blocking.
            """
            
            if self.state() != 'offline':
                self.__send_monitor_command('system_powerdown')
            else:
                raise self.InvalidStateChange('Invalid state for powerdown!')
            
        def signal_reset(self):
            """
            Sends the reset signal. This is non-blocking.
            """
            
            if self.state() != 'offline':
                self.__send_monitor_command('system_reset')
            else:
                raise self.InvalidStateChange('Invalid state for reset!')
            
        def stop(self):
            """
            Terminates the virtual machine.
            """
            
            if self.state() != 'offline':
                try:
                    self.__send_monitor_command('quit')
                except:
                    pass # Could not have initialized yet
                
                try:
                    os.kill(self.db_object.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass # Process does not exist
                
                self.__mark_offline()
            else:
                raise self.InvalidStateChange('Invalid state for stop()!')
            
        def state(self) -> str:
            """
            Return the current state of this virtual machine.
            
            Returns
            -------
            state :
                The current VM state, can be "offline" or "online"
            """
            
            self._state_check()
            
            if self.db_object.state != 'offline':
                self.audiopipe = self.db_object.audiopipe
            
            return self.db_object.state
            
        def stderr(self, *args, **kwargs):
            if self.db_object.handles:
                return os.fdopen(self.db_object.handles[0], *args, **kwargs)
            else:
                raise self.InvalidStateChange('Invalid state for stderr()!')
            
        def stdout(self, *args, **kwargs):
            if self.db_object.handles:
                return os.fdopen(self.db_object.handles[1], *args, **kwargs)
            else:
                raise self.InvalidStateChange('Invalid state for stdout()!')
            
        def _state_check(self):
            # Check if QEMU instance is actually still running
            if self.db_object.state != 'offline':
                try:
                    x = psutil.Process(self.db_object.pid)
                    
                    # Process may not immediately end
                    if x.status() == 'zombie':
                        x.kill()
                        
                        raise psutil.NoSuchProcess(self.db_object.pid)
                except psutil.NoSuchProcess:
                    self.__mark_offline()
            
        def __mark_offline(self):
            self.__file_cleanup()
            
            self.db_object.handles = None
            self.db_object.audiopipe = None
            self.db_object.pid   = None
            self.db_object.state = 'offline'
            self.db_session.commit()
            
            try:
                self.display.disconnect()
            except AttributeError:
                pass # Display not set up yet
            
        def __file_cleanup(self):
            self.audiopipe = None
            
            try:
                os.remove(self.db_object.audiopipe)
            except FileNotFoundError:
                pass # Already removed by something, perhaps a reboot
            
        def __send_monitor_command(self, command: str):
            with telnetlib.Telnet(
                GLOBAL_LOOPBACK,
                self.db_object.ports['monitor'],
                TELNET_TIMEOUT_SECS
            ) as session:
                session.read_until(b'(qemu) ')
                session.write(
                    '{0}'.format(command).encode('ascii') + b'\n'
                )
                session.read_until(b'(qemu) ')
                session.close()
            
        def __randomize_ports(self):
            self.db_object.ports = self.vertibird._new_ports()
            self.db_session.commit()
            
        def __random_device_id(self, length: int = 16):
            return ''.join(
                [random.choice(string.ascii_lowercase) for _ in range(length)]
            )
            
        def __set_option_offline(self):
            if self.state() != 'offline':
                raise self.InvalidStateChange('Must be offline to set options')
    
    class VertiVM(Base):
        """
        Internal database class, allows for the definition of virtual machines
        via SQLAlchemy.
        """
        
        __tablename__ = 'machines'
        
        id         = Column(String, primary_key=True)
        ports      = Column(PickleType)
        handles    = Column(PickleType)
        pid        = Column(Integer)
        audiopipe  = Column(String)
        state      = Column(String, default = 'offline')
        memory     = Column(Integer, default = 134217728)
        cores      = Column(Integer, default = 1)
        cpu        = Column(String, default = 'host')
        machine    = Column(String, default = 'pc')
        vga        = Column(String, default = 'std')
        sound      = Column(String, default = 'hda')
        bootorder  = Column(String, default = 'cdn')
        cdroms     = Column(PickleType, default = [])
        drives     = Column(PickleType, default = [])
        forwarding = Column(PickleType, default = [])
    
    def __find_free_port(self):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(('', 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return s.getsockname()[1]
    
    def _new_ports(self):
        return {
            'vnc': self.__find_free_port(),
            'monitor': self.__find_free_port()
        }
        
def session_generator(*args, **kwargs):
    return (lambda: Vertibird(*args, **kwargs))
        
class VertibirdSpawner(object):
    def __init__(self, *args, **kwargs):
        """
        Takes exactly the same arguments as Vertibird(), except it creates a
        decorator that automatically spawns Vertibird sessions for you.
        """
        
        self.vargs = args
        self.vkwargs = kwargs
    
    def vsession(self, func):
        """
        Decorator for spawning vertibird sessions. Creates the keyword
        argument "vertibird" for you. You will need to allow the definition of
        this argument when you define your function. It won't matter if you
        give it a default value.
        """
        
        def wrapper(*args, **kwargs):
            kwargs['vertibird'] = session_generator(
                *self.vargs,
                **self.vkwargs
            )()
            
            return func(*args, **kwargs)
            
        return wrapper
        
if __name__ == '__main__':
    import cv2
    import numpy as np
    import pyaudio
    import wave
    import queue
    
    vspawner = VertibirdSpawner()
    
    @vspawner.vsession
    def main(vertibird):
        global DEBUG
        DEBUG = True
        
        x = vertibird
        
        if len(x.list()) < 1:
            y = x.create()
        else:
            y = x.get(x.list()[-1])
        
        try:
            for dsk in y.list_cdroms():
                y.detach_cdrom(dsk)
            for dsk in y.list_drives():
                y.detach_drive(dsk['path'])
            
            y.attach_cdrom(
                '/home/naphtha/iso/win7x86.iso'
            )
            y.create_or_attach_drive(
                './drives/test.img',
                25769803776,
                'ide'
            )
            
            options = y.get_properties()
            options['memory'] = 2147483648
            options['cores'] = 4
            y.set_properties(options)
        except:
            pass
                        
        try:
            y.start()
        except Vertibird.VertiVMLive.InvalidStateChange:
            print('VM already running')
        
        print('Start non-blocking')
        
        imgGet = (lambda: cv2.cvtColor(np.asarray(
            y.display.capture().convert('RGB')
        ), cv2.COLOR_RGB2BGR))
        
        def agc(q, y):
            while True:
                grab = y.display.audio_grab()
                
                if len(grab) > 44:
                    q.put(grab)
                else:
                    time.sleep(STATE_CHECK_CLK_SECS)
                    
        dq = queue.Queue()            
        
        capthread = threading.Thread(
            target = agc, args = (dq, y), daemon = True
        ).start()
        
        p = pyaudio.PyAudio()
        stream = p.open(format=8,
                        channels=2,
                        rate=44100,
                        output=True)
        silence = chr(0) * 2 * 2 * AUDIO_CHUNKS
        while y.state() == 'online':
            try:
                f = io.BytesIO(dq.get_nowait())
            except:
                f = None
            
            if f:
                wf = wave.open(f)
                
                stream.write(wf.readframes(wf.getnframes()))
                
                wf.close()
                f.close()
            else:
                stream.write(silence)
                    
        stream.stop_stream()
        stream.close()
 
        """
        while y.state() == 'online':
            z = imgGet()
                
            cv2.imshow('image', z)
            cv2.waitKey(34)
            
            #i = (lambda i: 'None' if bool(i) == False else i)(input('>>> '))
            #print(eval(i))
        """
        
        y.wait()
        
        if y.state() == 'online':
            y.stop()
            
    main()
