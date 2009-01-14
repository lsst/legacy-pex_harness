#! /usr/bin/env python

import threading
import lsst.daf.base as dafBase
from lsst.daf.base import *

import lsst.ctrl.events as events
import time

if __name__ == "__main__":
    print "starting...\n"

    shutdownTopic = "triggerShutdownA"
    activemqBroker = "lsst8.ncsa.uiuc.edu"

    externalEventTransmitter = events.EventTransmitter(activemqBroker, shutdownTopic )

    root = PropertySet()

    externalEventTransmitter.publish(root)

