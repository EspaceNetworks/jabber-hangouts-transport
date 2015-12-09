import sys
sys.path.insert(0, './lib/hangups')
sys.path.insert(0, './lib/xmpp')
import os
import logging
import logging.handlers
import time
import signal
import traceback
import shelve
import xmlconfig
import xmpp
from xmpp.debug import Debug as XMPPDebug
import debug as debug_module

import config
import jh_hangups
from jh_hangups import HangupsManager
import jh_xmpp
from jh_xmpp import Transport, XMPPQueueThread, xmpp_lock


version = 'unknown'


def load_config():
    config_options = {}
    for configFile in config.configFiles:
        if os.path.isfile(configFile):
            xmlconfig.reloadConfig(configFile, config_options)
            config.configFile = configFile
            return
    sys.stderr.write(("Configuration file not found. "
                      "You need to create a config file and put it "
                      " in one of these locations:\n ") + "\n ".join(config.configFiles))
    sys.exit(1)


def sig_handler(signum, frame):
    transport.offlinemsg = 'Signal handler called with signal %s' % (signum,)
    logger.info('Signal handler called with signal %s' % (signum,))
    transport.online = 0


def log_error():
    err = '%s - %s\n' % (time.strftime('%a %d %b %Y %H:%M:%S'), version)
    if logfile is not None:
        logfile.write(err)
        traceback.print_exc(file=logfile)
        logfile.flush()
    sys.stderr.write(err)
    traceback.print_exc()


def setup_debugging():
    sys.path.append('/root/pycharm-debug-py3k.egg')
    import pydevd
    pydevd.settrace('192.168.4.47', port=5422, stdoutToServer=True, stderrToServer=True, suspend=False)

if __name__ == '__main__':
    setup_debugging()

    logfile = None
    if config.debugFile:
        logfile = open(config.debugFile, 'a')

    if config.debugXMPP:
        debug = ['always', 'nodebuilder']
    else:
        debug = []

    if config.saslUsername:
        sasl = 1
    else:
        config.saslUsername = config.jid
        sasl = 0

    connection = xmpp.client.Component(config.jid,
                                       config.port,
                                       debug=[],
                                       domains=[config.jid, config.confjid],
                                       sasl=sasl,
                                       bind=config.useComponentBinding,
                                       route=config.useRouteWrap)

    debug_module.setup_logging(connection)
    logger = logging.getLogger(__name__)
    logger.info("Jabber Hangouts transport is starting.")

    logging.debug("Starting Hangouts thread manager.")
    jh_hangups.hangups_manager = HangupsManager()
    jh_xmpp.userfile = shelve.open(config.spoolFile)

    logging.debug("Starting transport.")
    transport = Transport(connection, jh_xmpp.userfile)
    if not transport.xmpp_connect():
        logging.error("Could not connect to server, or password mismatch!")
        sys.exit(1)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    logging.debug("Starting transport queue thread.")
    XMPPQueueThread(transport).start()

    while transport.online:
        try:
            xmpp_lock.acquire()
            try:
                connection.Process(0.01)
            finally:
                xmpp_lock.release()
        except KeyboardInterrupt:
            _pendingException = sys.exc_info()
            raise _pendingException[0](_pendingException[1]).with_traceback(_pendingException[2])
        except IOError:
            transport.xmpp_disconnect()
        except:
            log_error()
        if not connection.isConnected():
            transport.xmpp_disconnect()

    if connection.isConnected():
        transport.xmpp_disconnect()
    jh_xmpp.userfile.close()
    connection.disconnect()

    logger.info('Main thread stopped.')

    if len(jh_hangups.hangups_manager.hangouts_threads) > 0:
        logger.warning('Tranport terminated, but Hangouts threads are still active.')
        for jid in jh_hangups.hangups_manager.hangouts_threads:
            jh_hangups.hangups_manager.send_message(jid, {'what': 'disconnect'})

    # Join the remaining threads and exit.
    sys.exit(0)
