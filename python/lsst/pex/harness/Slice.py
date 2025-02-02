#! /usr/bin/env python

# 
# LSST Data Management System
# Copyright 2008, 2009, 2010 LSST Corporation.
# 
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the LSST License Statement and 
# the GNU General Public License along with this program.  If not, 
# see <http://www.lsstcorp.org/LegalNotices/>.
#

from __future__ import with_statement

from lsst.pex.harness.Queue import Queue
from lsst.pex.harness.stage import StageProcessing
from lsst.pex.harness.stage import NoOpParallelProcessing
from lsst.pex.harness.Clipboard import Clipboard
from lsst.pex.harness.Directories import Directories
from lsst.pex.logging import Log, LogRec, Prop
from lsst.pex.logging import BlockTimingLog
from lsst.pex.harness import harnessLib as logutils

import lsst.pex.policy as policy
import lsst.pex.exceptions as ex

import lsst.daf.base as dafBase
from lsst.daf.base import *
import lsst.daf.persistence as dafPersist
from lsst.daf.persistence import *


import lsst.ctrl.events as events
import lsst.pex.exceptions
from lsst.pex.exceptions import *

import os, sys, signal, re, traceback, time, datetime
import threading
from threading import Event as PyEvent


"""
Slice represents a single parallel worker program.  
Slice executes the loop of Stages for processing a portion of an Image (e.g.,
single ccd or amplifier). The processing is synchonized with serial processing
in the main Pipeline.  
A Slice obtains its configuration by reading a policy file. 
"""

class Slice(object):
    '''Slice: Python Slice class implementation. '''

    #------------------------------------------------------------------------
    def __init__(self, runId="TEST", pipelinePolicyName=None, name="unnamed", rank=-1, workerId=None):
        """
        Initialize the Slice: create an empty Queue List and Stage List;
        Import the C++ Slice  and initialize the MPI environment
        """
        # log message levels
        self.TRACE = BlockTimingLog.INSTRUM+2
        self.VERB1 = self.TRACE
        self.VERB2 = self.VERB1 - 1
        self.VERB3 = self.VERB2 - 1
        self.log = None
        self.logthresh = None
        self._logdir = ""
        
        self._pipelineName = name
        
        self.queueList = []
        self.stageList = []
        self.stageClassList = []
        self.stagePolicyList = []
        self.sliceEventTopicList = []
        self.eventTopicList = []
        self.shareDataList = []
        self.shutdownTopic = "triggerShutdownEvent_slice"
        self.executionMode = 0
        self._runId = runId
        self.pipelinePolicyName = pipelinePolicyName

        self.cppLogUtils = logutils.LogUtils()
        self._rank = int(rank)

        if workerId is not None:
            self.workerId = workerId
        else:
            self.workerId = -1



    def __del__(self):
        """
        Delete the Slice object: cleanup 
        """
        if self.log is not None:
            self.log.log(self.VERB1, 'Python Slice being deleted')

    def initializeLogger(self):
        """
        Initialize the Logger after opening policy file 
        """
        if(self.pipelinePolicyName == None):
            self.pipelinePolicyName = "pipeline_policy.paf"
        dictName = "pipeline_dict.paf"
        topPolicy = policy.Policy.createPolicy(self.pipelinePolicyName)

        if (topPolicy.exists('execute')):
            self.executePolicy = topPolicy.get('execute')
        else:
            self.executePolicy = policy.Policy.createPolicy(self.pipelinePolicyName)

        # Check for eventBrokerHost 
        if (self.executePolicy.exists('eventBrokerHost')):
            self.eventBrokerHost = self.executePolicy.getString('eventBrokerHost')
        else:
            self.eventBrokerHost = "lsst8.ncsa.uiuc.edu"   # default value
        self.cppLogUtils.setEventBrokerHost(self.eventBrokerHost)

        doLogFile = self.executePolicy.getBool('localLogMode')
        self.cppLogUtils.initializeSliceLogger(doLogFile, self._pipelineName,
                                               self._runId, self._logdir,
                                               self._rank, self.workerId,
                                               BlockTimingLog.LINUXUDATA)

        # The log for use in the Python Slice
        self.log = self.cppLogUtils.getLogger()

        if (self.executePolicy.exists('logThreshold')):
            self.logthresh = self.executePolicy.get('logThreshold')
        else:
            if(self.logthresh == None):
                self.logthresh = self.TRACE
        self.setLogThreshold(self.logthresh)

        # self.log.addDestination(cout, Log.DEBUG);


    def configureSlice(self):
        """
        Configure the slice via reading a Policy file 
        """

        log = Log(self.log, "configureSlice")

        conflog = BlockTimingLog(self.log, "configureSlice", self.TRACE)
        conflog.start()

        stgcfg = self.executePolicy.getArray("appStage")

        self.stageNames = []
        for subpol in stgcfg:
            stageName = subpol.get("name")
            self.stageNames.append(stageName)

        self.executePolicy.loadPolicyFiles()

        # Obtain the working directory space locators  
        psLookup = lsst.daf.base.PropertySet()
        if (self.executePolicy.exists('dir')):
            dirPolicy = self.executePolicy.get('dir')
            shortName = None
            if (dirPolicy.exists('shortName')):
                shortName = dirPolicy.get('shortName')
            if shortName == None:
                shortName = self.pipelinePolicyName.split('.')[0]
            dirs = Directories(dirPolicy, shortName, self._runId)
            psLookup = dirs.getDirs()

        if (self.executePolicy.exists('database.url')):
            psLookup.set('dbUrl', self.executePolicy.get('database.url'))

        LogRec(log, self.VERB1) << "Configuring Slice"        \
                                << Prop("universeSize", self.universeSize) \
                                << Prop("runID", self._runId) \
                                << Prop("rank", self._rank)   \
                                << LogRec.endr;
        
        # Configure persistence logical location map with values for directory 
        # work space locators
        dafPersist.LogicalLocation.setLocationMap(psLookup)

        # Check for eventTimeout
        if (self.executePolicy.exists('eventTimeout')):
            self.eventTimeout = self.executePolicy.getInt('eventTimeout')
        else:
            self.eventTimeout = 10000000   # default value

        # Process Application Stages
        fullStageList = self.executePolicy.getArray("appStage")
        self.nStages = len(fullStageList)
        log.log(self.VERB2, "Found %d stages" % len(fullStageList))

        # extract the stage class name and associated policy file.  
        fullStageNameList = [ ]
        self.stagePolicyList = [ ]
        for stagei in xrange(self.nStages):
            fullStagePolicy = fullStageList[stagei]
            if (fullStagePolicy.exists('parallelClass')):
                parallelName = fullStagePolicy.getString('parallelClass')
                stagePolicy = fullStagePolicy.get('stagePolicy')
            else:
                parallelName = "lsst.pex.harness.stage.NoOpParallelProcessing"
                stagePolicy = None

            fullStageNameList.append(parallelName)
            self.stagePolicyList.append(stagePolicy)

            if self.stageNames[stagei] is None:
                self.stageNames[stagei] = fullStageNameList[-1].split('.')[-1]
            log.log(self.VERB3,
                    "Stage %d: %s: %s" % (stagei+1, self.stageNames[stagei],
                                          fullStageNameList[-1]))

        for astage in fullStageNameList:
            fullStage = astage.strip()
            tokenList = astage.split('.')
            classString = tokenList.pop()
            classString = classString.strip()

            package = ".".join(tokenList)

            # For example  package -> lsst.pex.harness.App1Stage  classString -> App1Stage
            AppStage = __import__(package, globals(), locals(), [classString], -1)
            StageClass = getattr(AppStage, classString)
            self.stageClassList.append(StageClass)

        log.log(self.VERB2, "Imported Stage Classes")

        #
        # Configure the Failure Stage
        #   - Read the policy information
        #   - Import failure stage Class and make failure stage instance Object
        #
        self.failureStageName = None
        self.failParallelName   = None
        if (self.executePolicy.exists('failureStage')):
            failstg = self.executePolicy.get("failureStage")
            self.failureStageName = failstg.get("name")

            if (failstg.exists('parallelClass')):
                self.failParallelName = failstg.getString('parallelClass')
                failStagePolicy = failstg.get('stagePolicy')
            else:
                self.failParallelName = "lsst.pex.harness.stage.NoOpParallelProcessing"
                failStagePolicy = None

            astage = self.failParallelName
            tokenList = astage.split('.')
            failClassString = tokenList.pop()
            failClassString = failClassString.strip()

            package = ".".join(tokenList)

            # For example  package -> lsst.pex.harness.App1Stage  classString -> App1Stage
            FailAppStage = __import__(package, globals(), locals(), [failClassString], -1)
            FailStageClass = getattr(FailAppStage, failClassString)

            sysdata = {}

            # sysdata["name"] = self._pipelineName
            sysdata["name"] = self.failureStageName
            sysdata["rank"] = self._rank
            sysdata["stageId"] = -1
            sysdata["universeSize"] = self.universeSize
            sysdata["runId"] =  self._runId

            if (failStagePolicy != None):
                self.failStageObject = FailStageClass(failStagePolicy, self.log, self.eventBrokerHost, sysdata)
                # (self, policy=None, log=None, eventBroker=None, sysdata=None, callSetup=True):
            else:
                self.failStageObject = FailStageClass(None, self.log, self.eventBrokerHost, sysdata)

            log.log(self.VERB2, "failureStage %s " % self.failureStageName)
            log.log(self.VERB2, "failParallelName %s " % self.failParallelName)


        # Process Event Topics
        self.eventTopicList = [ ]
        self.sliceEventTopicList = [ ]
        for item in fullStageList:
            self.eventTopicList.append(item.getString("eventTopic"))
            self.sliceEventTopicList.append(item.getString("eventTopic"))

        # Check for executionMode of oneloop 
        if (self.executePolicy.exists('executionMode') and (self.executePolicy.getString('executionMode') == "oneloop")):
            self.executionMode = 1

        # Process Share Data Schedule
        self.shareDataList = []
        for item in fullStageList:
            shareDataStage = False
            if (item.exists('shareData')):
                shareDataStage = item.getBool('shareData')
            self.shareDataList.append(shareDataStage)

        log.log(self.VERB3, "Loading in %d trigger topics" % \
                len(filter(lambda x: x != "None", self.eventTopicList)))
        for iStage in xrange(len(self.eventTopicList)):
            item = self.eventTopicList[iStage]
            if self.eventTopicList[iStage] != "None":
                log.log(self.VERB3, "eventTopic%d: %s" % (iStage+1, item))
            else:
                log.log(Log.DEBUG, "eventTopic%d: %s" % (iStage+1, item))

        count = 0
        for item in self.eventTopicList:
            newitem = "%s_%s" % (item, self._pipelineName)
            self.sliceEventTopicList[count] = newitem
            count += 1

        eventsSystem = events.EventSystem.getDefaultEventSystem()
        for topic in self.sliceEventTopicList:
            if (topic == "None_" + self._pipelineName):
                pass
            else:
                eventsSystem.createReceiver(self.eventBrokerHost, topic)
                log.log(self.VERB3, "Creating receiver %s" % (topic))


        conflog.done()

        log.log(self.VERB1, "Slice configuration complete");

    def initializeQueues(self):
        """
        Initialize the Queue List
        """
        log = Log(self.log, "initializeQueues")
        queuelog = BlockTimingLog(self.log, "initializeQueues", self.TRACE)
        queuelog.start()

        for iQueue in range(1, self.nStages+1+1):
            queue = Queue()
            self.queueList.append(queue)

        queuelog.done()

    def initializeStages(self):
        """
        Initialize the Stage List
        """
        log = Log(self.log, "initializeStages")

        istageslog = BlockTimingLog(self.log, "initializeStages", self.TRACE)
        istageslog.start()

        for iStage in range(1, self.nStages+1):
            # Make a Policy object for the Stage Policy file
            stagePolicy = self.stagePolicyList[iStage-1]
            # Make an instance of the specifies Application Stage
            # Use a constructor with the Policy as an argument
            StageClass = self.stageClassList[iStage-1]
            sysdata = {}
            # sysdata["name"] = self._pipelineName
            sysdata["name"] = self.stageNames[iStage-1]
            sysdata["rank"] = self._rank
            sysdata["stageId"] = iStage
            sysdata["universeSize"] = self.universeSize
            sysdata["runId"] =  self._runId
            # Here 
            if (stagePolicy != "None"):
                stageObject = StageClass(stagePolicy, self.log, self.eventBrokerHost, sysdata)
                # (self, policy=None, log=None, eventBroker=None, sysdata=None, callSetup=True):
            else:
                stageObject = StageClass(None, self.log, self.eventBrokerHost, sysdata)

            inputQueue  = self.queueList[iStage-1]
            outputQueue = self.queueList[iStage]

            # stageObject.setLookup(self._lookup)
            stageObject.initialize(outputQueue, inputQueue)
            self.stageList.append(stageObject)

        istageslog.done()

    def startInitQueue(self):
        """
        Place an empty Clipboard in the first Queue
        """
        clipboard = Clipboard()
        queue1 = self.queueList[0]
        queue1.addDataset(clipboard)

    def postOutputClipboard(self, iStage):
        """
        Place an empty Clipboard in the output queue for designated stage
        """
        clipboard = Clipboard()
        queue2 = self.queueList[iStage]
        queue2.addDataset(clipboard)

    def transferClipboard(self, iStage):
        """
        Move the Clipboard from the input queue to output queue for the designated stage
        """
        # clipboard = Clipboard()
        queue1 = self.queueList[iStage-1]
        queue2 = self.queueList[iStage]
        clipboard = queue1.getNextDataset()
        queue2.addDataset(clipboard)

    def startStagesLoop(self): 
        """
        Execute the Stage loop. The loop progressing in step with 
        the analogous stage loop in the central Pipeline by means of
        MPI Bcast and Barrier calls.
        """
        startStagesLoopLog = self.log.timeBlock("startStagesLoop", self.TRACE)
        looplog = BlockTimingLog(self.log, "visit", self.TRACE)
        stagelog = BlockTimingLog(looplog, "stage", self.TRACE)

        self.log.log(Log.INFO, "Begin startStagesLoopLog")

        self.threadBarrier()

        visitcount = 0
        while True:
            self.log.log(Log.INFO, "visitcount %d %s " %  (visitcount, datetime.datetime.now()))

            if ((self.executionMode == 1) and (visitcount == 1)):
                LogRec(looplog, Log.INFO)  << "terminating Slice Stage Loop "
                # self.cppPipeline.invokeShutdown()
                break

            visitcount += 1
            looplog.setPreamblePropertyInt("LOOPNUM", visitcount)

            stagelog.setPreamblePropertyInt("LOOPNUM", visitcount)
            # stagelog.setPreamblePropertyInt("stagename", visitcount)
            timesVisitStart = os.times()

            # looplog.setPreamblePropertyFloat("usertime", timesVisitStart[0])
            # looplog.setPreamblePropertyFloat("systemtime", timesVisitStart[1])
            looplog.setPreamblePropertyDouble("usertime", timesVisitStart[0])
            looplog.setPreamblePropertyDouble("systemtime", timesVisitStart[1])
            looplog.start()

            self.startInitQueue()    # place an empty clipboard in the first Queue

            self.errorFlagged = 0
            for iStage in range(1, self.nStages+1):
                stagelog.setPreamblePropertyInt("STAGEID", iStage)
                stagelog.setPreamblePropertyString("stagename", self.stageNames[iStage-1])
                stagelog.start(self.stageNames[iStage-1] + " loop")
                stagelog.log(Log.INFO, "Begin stage loop iteration iStage %d " % iStage)

                stageObject = self.stageList[iStage-1]
                self.handleEvents(iStage, stagelog)

                # synchronize before preprocess
                self.threadBarrier()

                # synchronize after preprocess, before process
                self.threadBarrier()

                self.tryProcess(iStage, stageObject, stagelog)

                # synchronize after process, before postprocess
                self.threadBarrier()

                # synchronize after postprocess
                self.threadBarrier()

                stagelog.log(self.TRACE, "End stage loop iteration iStage %d " % iStage)
                stagelog.log(Log.INFO, "End stage loop iteration : ErrorCheck \
                   iStage %d stageName %s errorFlagged_%d " % (iStage, self.stageNames[iStage-1], self.errorFlagged) )

                stagelog.done()

            looplog.log(self.VERB2, "Completed Stage Loop")

            # If no error/exception was flagged, then clear the final Clipboard in the final Queue
            if self.errorFlagged == 0:
                looplog.log(Log.DEBUG,
                            "Retrieving final Clipboard for deletion")
                finalQueue = self.queueList[self.nStages]
                finalClipboard = finalQueue.getNextDataset()
                finalClipboard.close()
                del finalClipboard
                looplog.log(Log.DEBUG, "Deleted final Clipboard")
            else:
                looplog.log(self.VERB3, "Error flagged on this visit")

            timesVisitDone = os.times()
            utime = timesVisitDone[0] - timesVisitStart[0]
            stime = timesVisitDone[1] - timesVisitStart[1]
            wtime = timesVisitDone[4] - timesVisitStart[4]
            totalTime = utime + stime
            looplog.log(Log.INFO, "visittimes : utime %.4f stime %.4f  total %.4f wtime %.4f" % (utime, stime, totalTime, wtime) )

            # looplog.setPreamblePropertyFloat("usertime", timesVisitDone[0])
            # looplog.setPreamblePropertyFloat("systemtime", timesVisitDone[1])
            looplog.setPreamblePropertyDouble("usertime", timesVisitDone[0])
            looplog.setPreamblePropertyDouble("systemtime", timesVisitDone[1])
            looplog.done()

            try:
                memmsg = "mem:"
                with open("/proc/%d/status" % os.getpid(), "r") as f:
                    for l in f:
                        m = re.match(r'Vm(Size|RSS|Peak|HWM):\s+(\d+ \wB)', l)
                        if m:
                            memmsg += " %s=%s" % m.groups()
                looplog.log(Log.INFO, memmsg)
            except:
                pass

            # LogRec(looplog, Log.INFO) << Prop("usertime", utime) \
            #                            << Prop("systemtime", stime) \
            #                           << LogRec.endr;

        startStagesLoopLog.done()

    def threadBarrier(self):
        """
        Create an approximate barrier where all Slices intercommunicate with the Pipeline 
        """

        log = Log(self.log, "threadBarrier")

        entryTime = time.time()
        log.log(Log.DEBUG, "Slice %d waiting for signal from Pipeline %f" % (self._rank, entryTime))

        self.loopEventA.wait()

        signalTime1 = time.time()
        log.log(Log.DEBUG, "Slice %d done waiting; signaling back %f" % (self._rank, signalTime1))

        if(self.loopEventA.isSet()):
            self.loopEventA.clear()

        self.loopEventB.set()

        signalTime2 = time.time()
        log.log(Log.DEBUG, "Slice %d sent signal back. Exit threadBarrier  %f" % (self._rank, signalTime2))

    def shutdown(self): 
        """
        Shutdown the Slice execution
        """
        shutlog = Log(self.log, "shutdown", Log.INFO);
        pid = os.getpid()
        shutlog.log(Log.INFO, "Shutting down Slice:  pid " + str(pid))
        os.kill(pid, signal.SIGKILL) 

    def tryProcess(self, iStage, stage, stagelog):
        """
        Executes the try/except construct for Stage process() call 
        """
        # Important try - except construct around stage process() 
        proclog = stagelog.timeBlock("tryProcess", self.TRACE-2);

        stageObject = self.stageList[iStage-1]
        proclog.log(self.VERB3, "Getting process signal from Pipeline")

        # Important try - except construct around stage process() 
        try:
            # If no error/exception has been flagged, run process()
            # otherwise, simply pass along the Clipboard 
            if (self.errorFlagged == 0):
                processlog = stagelog.timeBlock("process", self.TRACE)
                stageObject.applyProcess()

                outputQueue = self.queueList[iStage]
                clipboard = outputQueue.element()
                proclog.log(Log.INFO, "Checking_For_Shotdown")

                if clipboard.has_key("noMoreDatasets"): 
                    proclog.log(Log.INFO, "Ready_For_Shutdown")
                    self.shutdown();

                processlog.done()
            else:
                proclog.log(self.TRACE, "Skipping process due to error")
                self.transferClipboard(iStage)
  
        except:
            trace = "".join(traceback.format_exception(
                sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2]))
            proclog.log(Log.FATAL, trace)

            # Flag that an exception occurred to guide the framework to skip processing
            self.errorFlagged = 1
            # Post the cliphoard that the Stage failed to transfer to the output queue

            if(self.failureStageName != None):
                if(self.failParallelName != "lsst.pex.harness.stage.NoOpParallelProcessing"):

                    LogRec(proclog, self.VERB2) << "failureStageName exists "    \
                                           << self.failureStageName     \
                                           << "and failParallelName exists "    \
                                           << self.failParallelName     \
                                           << LogRec.endr;

                    inputQueue  = self.queueList[iStage-1]
                    outputQueue = self.queueList[iStage]

                    clipboard = inputQueue.element()
                    clipboard.put("failedInStage",  stage.getName())
                    clipboard.put("failedInStageN", iStage)
                    clipboard.put("failureType", str(sys.exc_info()[0]))
                    clipboard.put("failureMessage", str(sys.exc_info()[1]))
                    clipboard.put("failureTraceback", trace)

                    self.failStageObject.initialize(outputQueue, inputQueue)

                    self.failStageObject.applyProcess()

                    proclog.log(self.TRACE, "Popping off failure stage Clipboard")
                    clipboard = outputQueue.getNextDataset()
                    clipboard.close()
                    del clipboard
                    proclog.log(self.TRACE, "Erasing and deleting failure stage Clipboard")

                else:
                    proclog.log(self.VERB2, "No ParallelProcessing to do for failure stage")

            self.postOutputClipboard(iStage)

        proclog.log(self.VERB3, "Getting end of process signal from Pipeline")
        proclog.done()

    def handleEvents(self, iStage, stagelog):
        """
        Handles Events: transmit or receive events as specified by Policy
        """
        log = stagelog.timeBlock("handleEvents", self.TRACE-2)
        eventsSystem = events.EventSystem.getDefaultEventSystem()

        thisTopic = self.eventTopicList[iStage-1]

        if (thisTopic != "None"):
            log.log(self.VERB3, "Processing topic: " + thisTopic)
            sliceTopic = self.sliceEventTopicList[iStage-1]

            waitlog = log.timeBlock("eventwait " + sliceTopic, self.TRACE,
                                    "wait for event...")

            # Receive the event from the Pipeline 
            # Call with a timeout , followed by a call to time sleep to free the GIL 
            # periodically

            sleepTimeout = 0.1
            transTimeout = 900

            inputParamPropertySetPtr = eventsSystem.receive(sliceTopic, transTimeout)
            while(inputParamPropertySetPtr == None):
                time.sleep(sleepTimeout)
                inputParamPropertySetPtr = eventsSystem.receive(sliceTopic, transTimeout)
     

            waitlog.done()
            LogRec(log, self.TRACE) << "received event; contents: "        \
                                << inputParamPropertySetPtr \
                                << LogRec.endr


            self.populateClipboard(inputParamPropertySetPtr, iStage, thisTopic)
            log.log(self.VERB3, 'Received event; added payload to clipboard')
        else:
            log.log(Log.DEBUG, 'No event to handle')

        log.done()

    def populateClipboard(self, inputParamPropertySetPtr, iStage, eventTopic):
        """
        Place the event payload onto the Clipboard
        """
        log = Log(self.log, "populateClipboard");
        log.log(Log.DEBUG,'Python Pipeline populateClipboard');

        queue = self.queueList[iStage-1]
        clipboard = queue.element()

        # Slice does not disassemble the payload of the event. 
        # It knows nothing of the contents. 
        # It simply places the payload on the clipboard with key of the eventTopic
        clipboard.put(eventTopic, inputParamPropertySetPtr)

    #------------------------------------------------------------------------
    def getRun(self):
        """
        get method for the runId
        """
        return self._runId

    #------------------------------------------------------------------------
    def setRun(self, run):
        """
        set method for the runId
        """
        self._runId = run

    def getLogThreshold(self):
        """
        return the default message importance threshold being used for 
        recording messages.  The returned value reflects the threshold 
        associated with the default root (system-wide) logger (or what it will
        be after logging is initialized).  Some underlying components may 
        override this threshold.
        @return int   the threshold value as would be returned by 
                         Log.getThreshold()
        """
        if self.log is None:
            return self.logthresh
        else:
            return Log.getDefaultLog().getThreshold()

    def setLogThreshold(self, level):
        """
        set the default message importance threshold to be used for 
        recording messages.  This will value be applied to the default
        root (system-wide) logger (or what it will be after logging is 
        initialized) so that all software components are affected.
        @param level   the threshold level as expected by Log.setThreshold().
        """
        if self.log is not None:
            Log.getDefaultLog().setThreshold(level)
            self.log.log(Log.INFO, 
                         "Upating Root Log Message Threshold to %i" % level)
        self.logthresh = level

    def setLogDir(self, logdir):
        """
        set the default directory into which the slice should write log files 
        @param logdir   the directory in which log files should be written
        """
        if (logdir == "None" or logdir == None):
            self._logdir = ""
        else:
            self._logdir = logdir

    def makeStageName(self, appStagePolicy):
        if appStagePolicy.getValueType("stagePolicy") == appStagePolicy.FILE:
            pfile = os.path.splitext(os.path.basename(
                        appStagePolicy.getFile("stagePolicy").getPath()))[0]
            return trailingpolicy.sub('', pfile)
        else:
            return None
        
    def setLoopEventA(self, event):
        self.loopEventA = event

    def setLoopEventB(self, event):
        self.loopEventB = event

    def setUniverseSize(self, usize):
        self.universeSize = usize

trailingpolicy = re.compile(r'_*(policy|dict)$', re.IGNORECASE)


