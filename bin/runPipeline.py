#! /usr/bin/env python
"""
Usage: runPipeline.py [-L lev] policy runID

Execute the pipeline described by the given policy, assigning it
the given run ID.  This script should only be launched via mpiexec.  

If the pipeline policy references other policy files (via the "@" file 
include format), it is assumed that the given path is relative to the 
current working directory where this script is executed. 
"""
from lsst.pex.harness.Queue import Queue
from lsst.pex.harness.Stage import Stage
from lsst.pex.harness.Clipboard import Clipboard
from lsst.pex.harness.Pipeline import Pipeline
import lsst.pex.harness.run as run

import lsst.pex.policy as policy
from lsst.pex.logging import Log

import lsst.daf.base as dafBase
from lsst.daf.base import PropertySet

import lsst.ctrl.events as events

import os
import sys
import optparse, traceback

usage = """Usage: %prog [-dvqs] [-V lev] policy runID"""
desc = """Execute the pipeline described by the given policy, assigning it
the given run ID.
"""

cl = optparse.OptionParser(usage=usage, description=desc)
run.addVerbosityOption(cl)

# ditch
cl.add_option("-d", "--debug", action="store_const", const='debug',
              dest="verbosity",
              help="print maximum amount of messages")
cl.add_option("-v", "--verbose", action="store_const", const='verb3',
              dest="verbosity",
              help="print extra messages (same as -L verb3)")
cl.add_option("-q", "--quiet", action="store_const", const='quiet',
              dest="verbosity",
              help="print only warning & error messages")
cl.add_option("-s", "--silent", action="store_const", const='silent',
              dest="verbosity",
              help="print nothing (if possible)")
#

def main():
    """parse the input arguments and execute the pipeline
    """

    (cl.opts, cl.args) = cl.parse_args()
    logthresh = run.verbosity2threshold(cl.opts.verbosity)

    if(len(cl.args) < 2):
        print >> sys.stderr, \
            "%s: missing required argument(s)." % cl.get_prog_name()
        print cl.get_usage()
        sys.exit(1)

    pipelinePolicyName = cl.args[0]
    runId = cl.args[1]

    runPipeline(pipelinePolicyName, runId, logthresh)

def runPipeline(policyFile, runId, logthresh=None):
    """
    Create the Pipeline object and start the pipeline execution
    @param policyFile   the name of the policy file that defines the pipeline
    @param runId        the string to use as the production run identifier
    @param logthresh    the logging threshold to use to control the verbosity
                           of messages.
    """
    print policyFile, runId, logthresh
    sys.exit(0)

    pyPipeline = Pipeline(runId, policyFile)
    if isinstance(logthresh, int):
        pyPipeline.setThreshold(logthresh)

    pyPipeline.configurePipeline()   

    pyPipeline.initializeQueues()  

    pyPipeline.initializeStages()    

    pyPipeline.startSlices()  

    # pyPipeline.startInitQueue()    # place an empty clipboard in the first Queue 

    pyPipeline.startStagesLoop()

    pyPipeline.shutdown()



if (__name__ == '__main__'):
    try:
        main()
    except run.UsageError, e:
        print >> sys.stderr, "%s: %s" % (cl.get_prog_name(), e)
        sys.exit(1)
    except Exception, e:
        log = Log(Log.getDefaultLog(),"runPipeline")
        log.log(Log.FATAL, str(e))
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)
