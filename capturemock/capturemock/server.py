
import optparse, os, stat, sys, logging, logging.config, shutil, socket, subprocess, types, threading, time, inspect, re, difflib
from SocketServer import TCPServer, StreamRequestHandler
from copy import copy
from ordereddict import OrderedDict

def create_option_parser():
    usage = """usage: %prog [options] 

Standalone traffic server program. Basic usage is to grab the
address it writes out and run a program with TEXTTEST_MIM_SERVER set to it.
traffic_cmd.py can then intercept command-line programs, traffic_pymodule.py can
intercept python modules while the system itself can be modified to "internally"
react to the above module to repoint where it sends socket interactions"""

    parser = optparse.OptionParser(usage)
    parser.add_option("-a", "--asynchronous-file-edit-commands", metavar="ENV",
                      help="Commands which may cause files to be edited after they have exited (presumably via background processes they start)")
    parser.add_option("-A", "--alter-response", metavar="REPLACEMENTS",
                      help="Response alterations to perform on the text before recording or returning it")
    parser.add_option("-e", "--transfer-environment", metavar="ENV",
                      help="Environment variables that are significant to particular programs and should be recorded if changed.")
    parser.add_option("-i", "--ignore-edits", metavar="FILES",
                      help="When monitoring which files have been edited by a program, ignore files and directories with the given names")
    parser.add_option("-p", "--replay", 
                      help="replay traffic recorded in FILE.", metavar="FILE")
    parser.add_option("-I", "--replay-items", 
                      help="attempt replay only items in ITEMS, record the rest", metavar="ITEMS")
    parser.add_option("-l", "--logdefaults",
                      help="Default values to pass to log configuration file. Only useful with -L", metavar="LOGDEFAULTS")
    parser.add_option("-L", "--logconfigfile",
                      help="Configure logging via the log configuration file at FILE.", metavar="LOGCONFIGFILE")
    parser.add_option("-f", "--replay-file-edits", 
                      help="restore edited files referred to in replayed file from DIR.", metavar="DIR")
    parser.add_option("-m", "--python-module-intercepts", 
                      help="Python modules whose objects should be stored locally rather than returned as they are", metavar="MODULES")
    parser.add_option("-r", "--record", 
                      help="record traffic to FILE.", metavar="FILE")
    parser.add_option("-F", "--record-file-edits", 
                      help="store edited files under DIR.", metavar="DIR")
    parser.add_option("-s", "--sequential-mode", action="store_true",
                      help="Disable concurrent traffic, handle all incoming messages sequentially")
    parser.add_option("-t", "--test-path", metavar="PATH", 
                      help="Set a test path name for TextTest filtering and/or error messages")
    return parser

def parseCmdDictionary(cmdStr, listvals):
    dict = {}
    if cmdStr:
        for part in cmdStr.split(","):
            cmd, varString = part.split("=")
            if listvals:
                dict[cmd] = varString.split("+")
            else:
                dict[cmd] = varString
    return dict


class TrafficServer(TCPServer):
    def __init__(self, options):
        self.useThreads = not options.sequential_mode
        self.filesToIgnore = []
        if options.ignore_edits:
            self.filesToIgnore = options.ignore_edits.split(",")
        self.recordFileHandler = RecordFileHandler(options.record)
        self.replayInfo = ReplayInfo(options.replay, options.replay_items)
        self.requestCount = 0
        self.diag = logging.getLogger("Traffic Server")
        self.topLevelForEdit = [] # contains only paths explicitly given. Always present.
        self.fileEditData = OrderedDict() # contains all paths, including subpaths of the above. Empty when replaying.
        self.terminate = False
        self.hasAsynchronousEdits = False
        TCPServer.__init__(self, (socket.gethostname(), 0), TrafficRequestHandler)
        host, port = self.socket.getsockname()
        sys.stdout.write(host + ":" + str(port) + "\n") # Tell our caller, so they can tell the program being handled
        sys.stdout.flush()
        
    def run(self):
        self.diag.info("Starting traffic server")
        while not self.terminate:
            self.handle_request()
        # Join all remaining request threads so they don't
        # execute after Python interpreter has started to shut itself down.
        for t in threading.enumerate():
            if t.name == "request":
                t.join()
        self.diag.info("Shut down traffic server")
            
    def shutdown(self):
        self.diag.info("Told to shut down!")
        if self.useThreads:
            # Setting terminate will only work if we do it in the main thread:
            # otherwise the main thread might be in a blocking call at the time
            # So we reset the thread flag and send a new message
            self.useThreads = False
            sendSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sendSocket.connect(self.socket.getsockname())
            sendSocket.sendall("TERMINATE_SERVER\n")
            sendSocket.shutdown(2)
        else:
            self.terminate = True
        
    def process_request_thread(self, request, client_address, requestCount):
        # Copied from ThreadingMixin, more or less
        # We store the order things appear in so we know what order they should go in the file
        try:
            TrafficRequestHandler(requestCount, request, client_address, self)
            self.close_request(request)
        except: # pragma : no cover - interpreter code in theory...
            self.handle_error(request, client_address)
            self.close_request(request)

    def process_request(self, request, client_address):
        self.requestCount += 1
        if self.useThreads:
            """Start a new thread to process the request."""
            t = threading.Thread(target = self.process_request_thread, name="request",
                                 args = (request, client_address, self.requestCount))
            t.start()
        else:
            self.process_request_thread(request, client_address, self.requestCount)
        
    def findFilesAndLinks(self, path):
        if not os.path.exists(path):
            return []
        if os.path.isfile(path) or os.path.islink(path):
            return [ path ]

        paths = []
        for srcroot, srcdirs, srcfiles in os.walk(path):
            for fileToIgnore in self.filesToIgnore:
                if fileToIgnore in srcdirs:
                    srcdirs.remove(fileToIgnore)
                if fileToIgnore in srcfiles:
                    srcfiles.remove(fileToIgnore)
            for srcfile in srcfiles:
                paths.append(os.path.join(srcroot, srcfile))

            for srcdir in srcdirs:
                fullSrcPath = os.path.join(srcroot, srcdir)
                if os.path.islink(fullSrcPath):
                    paths.append(fullSrcPath)
        return paths

    def getLatestModification(self, path):
        if os.path.exists(path):
            statObj = os.stat(path)
            return statObj[stat.ST_MTIME], statObj[stat.ST_SIZE]
        else:
            return None, 0
        
    def addPossibleFileEdits(self, traffic):
        allEdits = traffic.findPossibleFileEdits()
        for file in allEdits:
            if file in self.topLevelForEdit:
                self.topLevelForEdit.remove(file)
            # Always move them to the beginning, most recent edits are most relevant
            self.topLevelForEdit.insert(0, file)

            # edit times aren't interesting when doing pure replay
            if not self.replayInfo.isActiveForAll():
                for subPath in self.findFilesAndLinks(file):                
                    modTime, modSize = self.getLatestModification(subPath)
                    self.fileEditData[subPath] = modTime, modSize
                    self.diag.info("Adding possible sub-path edit for " + subPath + " with mod time " +
                                   time.strftime("%d%b%H:%M:%S", time.localtime(modTime)) + " and size " + str(modSize))
        return len(allEdits) > 0
    
    def process(self, traffic, reqNo):
        if not self.replayInfo.isActiveFor(traffic):
            # If we're recording, check for file changes before we do
            # Must do this before as they may be a side effect of whatever it is we're processing
            for fileTraffic in self.getLatestFileEdits():
                self._process(fileTraffic, reqNo)

        self._process(traffic, reqNo)
        self.hasAsynchronousEdits |= traffic.makesAsynchronousEdits()
        self.recordFileHandler.requestComplete(reqNo)
        if not self.hasAsynchronousEdits:
            # Unless we've marked it as asynchronous we start again for the next traffic.
            self.topLevelForEdit = []
            self.fileEditData = OrderedDict()
        
    def _process(self, traffic, reqNo):
        self.diag.info("Processing traffic " + traffic.__class__.__name__)
        hasFileEdits = self.addPossibleFileEdits(traffic)
        responses = self.getResponses(traffic, hasFileEdits)
        shouldRecord = not traffic.enquiryOnly(responses)
        if shouldRecord:
            traffic.record(self.recordFileHandler, reqNo)
        for response in responses:
            self.diag.info("Response of type " + response.__class__.__name__ + " with text " + repr(response.text))
            if shouldRecord:
                response.record(self.recordFileHandler, reqNo)
            for chainResponse in response.forwardToDestination():
                self._process(chainResponse, reqNo)
            self.diag.info("Completed response of type " + response.__class__.__name__)            

    def getResponses(self, traffic, hasFileEdits):
        if self.replayInfo.isActiveFor(traffic):
            self.diag.info("Replay active for current command")
            replayedResponses = []
            filesMatched = []
            for responseClass, text in self.replayInfo.readReplayResponses(traffic):
                responseTraffic = self.makeResponseTraffic(traffic, responseClass, text, filesMatched)
                if responseTraffic:
                    replayedResponses.append(responseTraffic)
            return traffic.filterReplay(replayedResponses)
        else:
            trafficResponses = traffic.forwardToDestination()
            if hasFileEdits: # Only if the traffic itself can produce file edits do we check here
                return self.getLatestFileEdits() + trafficResponses
            else:
                return trafficResponses

    def getFileBeingEdited(self, givenName, fileType, filesMatched):
        # drop the suffix which is internal to TextTest
        fileName = givenName.split(".edit_")[0]
        bestMatch, bestScore = None, -1
        for editedFile in self.topLevelForEdit:
            if (fileType == "directory" and os.path.isfile(editedFile)) or \
               (fileType == "file" and os.path.isdir(editedFile)):
                continue

            editedName = os.path.basename(editedFile)
            if editedName == fileName and editedFile not in filesMatched:
                filesMatched.append(editedFile)
                bestMatch = editedFile
                break
            else:
                matchScore = self.getFileMatchScore(fileName, editedName)
                if matchScore > bestScore:
                    bestMatch, bestScore = editedFile, matchScore

        if bestMatch and bestMatch.startswith("/cygdrive"): # on Windows, paths may be referred to by cygwin path, handle this
            bestMatch = bestMatch[10] + ":" + bestMatch[11:]
        return bestMatch

    def getFileMatchScore(self, givenName, actualName):
        if actualName.find(".edit_") != -1:
            return -1

        return self._getFileMatchScore(givenName, actualName, lambda x: x) + \
               self._getFileMatchScore(givenName, actualName, lambda x: -1 -x)
    
    def _getFileMatchScore(self, givenName, actualName, indexFunction):
        score = 0
        while len(givenName) > score and len(actualName) > score and givenName[indexFunction(score)] == actualName[indexFunction(score)]:
            score += 1
        return score

    def makeResponseTraffic(self, traffic, responseClass, text, filesMatched):
        if responseClass is FileEditTraffic:
            fileName = text.strip()
            storedFile, fileType = FileEditTraffic.getFileWithType(fileName)
            if storedFile:
                editedFile = self.getFileBeingEdited(fileName, fileType, filesMatched)
                if editedFile:
                    self.diag.info("File being edited for '" + fileName + "' : will replace " + str(editedFile) + " with " + str(storedFile))
                    changedPaths = self.findFilesAndLinks(storedFile)
                    return FileEditTraffic(fileName, editedFile, storedFile, changedPaths, reproduce=True)
        else:
            return responseClass(text, traffic.responseFile)

    def findRemovedPath(self, removedPath):
        # We know this path is removed, what about its parents?
        # We want to store the most concise removal.
        parent = os.path.dirname(removedPath)
        if os.path.exists(parent):
            return removedPath
        else:
            return self.findRemovedPath(parent)

    def getLatestFileEdits(self):
        traffic = []
        removedPaths = []
        for file in self.topLevelForEdit:
            changedPaths = []
            newPaths = self.findFilesAndLinks(file)
            for subPath in newPaths:
                newEditInfo = self.getLatestModification(subPath)
                if newEditInfo != self.fileEditData.get(subPath):
                    changedPaths.append(subPath)
                    self.fileEditData[subPath] = newEditInfo

            for oldPath in self.fileEditData.keys():
                if (oldPath == file or oldPath.startswith(file + "/")) and oldPath not in newPaths:
                    removedPath = self.findRemovedPath(oldPath)
                    self.diag.info("Deletion of " + oldPath + "\n - registering " + removedPath)
                    removedPaths.append(oldPath)
                    if removedPath not in changedPaths:
                        changedPaths.append(removedPath)
                    
            if len(changedPaths) > 0:
                traffic.append(FileEditTraffic.makeRecordedTraffic(file, changedPaths))

        for path in removedPaths:
            del self.fileEditData[path]

        return traffic


class Traffic(object):
    def __init__(self, text, responseFile):
        self.text = text
        self.responseFile = responseFile

    def findPossibleFileEdits(self):
        return []
    
    def hasInfo(self):
        return len(self.text) > 0

    def isMarkedForReplay(self, replayItems):
        return True # Some things can't be disabled and hence can't be added on piecemeal afterwards

    def getDescription(self):
        return self.direction + self.typeId + ":" + self.text

    def makesAsynchronousEdits(self):
        return False
    
    def enquiryOnly(self, responses=[]):
        return False
    
    def write(self, message):
        if self.responseFile:
            try:
                self.responseFile.write(message)
            except socket.error:
                # The system under test has died or is otherwise unresponsive
                # Should handle this, probably. For now, ignoring it is better than stack dumps
                pass
                
    def forwardToDestination(self):
        self.write(self.text)
        if self.responseFile:
            self.responseFile.close()
        return []

    def record(self, recordFileHandler, reqNo):
        if not self.hasInfo():
            return
        desc = self.getDescription()
        if not desc.endswith("\n"):
            desc += "\n"
        recordFileHandler.record(desc, reqNo)

    def filterReplay(self, trafficList):
        return trafficList

    
class ResponseTraffic(Traffic):
    direction = "->"

class StdoutTraffic(ResponseTraffic):
    typeId = "OUT"
    def forwardToDestination(self):
        self.write(self.text + "|TT_CMD_SEP|")
        return []

class StderrTraffic(ResponseTraffic):
    typeId = "ERR"
    def forwardToDestination(self):
        self.write(self.text + "|TT_CMD_SEP|")
        return []

class SysExitTraffic(ResponseTraffic):
    typeId = "EXC"
    def __init__(self, status, responseFile):
        ResponseTraffic.__init__(self, str(status), responseFile)
        self.exitStatus = int(status)
    def hasInfo(self):
        return self.exitStatus != 0

class FileEditTraffic(ResponseTraffic):
    typeId = "FIL"
    linkSuffix = ".TEXTTEST_SYMLINK"
    deleteSuffix = ".TEXTTEST_DELETION"
    replayFileEditDir = None
    recordFileEditDir = None
    fileRequestCount = {} # also only for recording
    diag = None
    @classmethod
    def configure(cls, options):
        cls.diag = logging.getLogger("Traffic Server")
        cls.replayFileEditDir = options.replay_file_edits
        cls.recordFileEditDir = options.record_file_edits
        
    def __init__(self, fileName, activeFile, storedFile, changedPaths, reproduce):
        self.activeFile = activeFile
        self.storedFile = storedFile
        self.changedPaths = changedPaths
        self.reproduce = reproduce
        ResponseTraffic.__init__(self, fileName, None)

    @classmethod
    def getFileWithType(cls, fileName):
        if cls.replayFileEditDir:
            for name in [ fileName, fileName + cls.linkSuffix, fileName + cls.deleteSuffix ]:
                candidate = os.path.join(cls.replayFileEditDir, name)
                if os.path.exists(candidate):
                    return candidate, cls.getFileType(candidate)
        return None, "unknown"

    @classmethod
    def getFileType(cls, fileName):
        if fileName.endswith(cls.deleteSuffix):
            return "unknown"
        elif os.path.isdir(fileName):
            return "directory"
        else:
            return "file"

    @classmethod
    def makeRecordedTraffic(cls, file, changedPaths):
        storedFile = os.path.join(cls.recordFileEditDir, cls.getFileEditName(os.path.basename(file)))
        fileName = os.path.basename(storedFile)
        cls.diag.info("File being edited for '" + fileName + "' : will store " + str(file) + " as " + str(storedFile))
        for path in changedPaths:
            cls.diag.info("- changed " + path)
        return cls(fileName, file, storedFile, changedPaths, reproduce=False)

    @classmethod
    def getFileEditName(cls, name):
        timesUsed = cls.fileRequestCount.setdefault(name, 0) + 1
        cls.fileRequestCount[name] = timesUsed
        if timesUsed > 1:
            name += ".edit_" + str(timesUsed)
        return name

    def removePath(self, path):
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)

    def copy(self, srcRoot, dstRoot):
        for srcPath in self.changedPaths:
            dstPath = srcPath.replace(srcRoot, dstRoot)
            try:
                dstParent = os.path.dirname(dstPath)
                if not os.path.isdir(dstParent):
                    os.makedirs(dstParent)
                if srcPath.endswith(self.linkSuffix):
                    self.restoreLink(srcPath, dstPath.replace(self.linkSuffix, ""))
                elif os.path.islink(srcPath):
                    self.storeLinkAsFile(srcPath, dstPath + self.linkSuffix)
                elif srcPath.endswith(self.deleteSuffix):
                    self.removePath(dstPath.replace(self.deleteSuffix, ""))
                elif not os.path.exists(srcPath):
                    open(dstPath + self.deleteSuffix, "w").close()
                else:
                    shutil.copyfile(srcPath, dstPath)
            except IOError:
                print "Could not transfer", srcPath, "to", dstPath

    def restoreLink(self, srcPath, dstPath):
        linkTo = open(srcPath).read().strip()
        if not os.path.islink(dstPath):
            os.symlink(linkTo, dstPath)

    def storeLinkAsFile(self, srcPath, dstPath):
        writeFile = open(dstPath, "w")
        # Record relative links as such
        writeFile.write(os.readlink(srcPath).replace(os.path.dirname(srcPath) + "/", "") + "\n")
        writeFile.close()

    def forwardToDestination(self):
        self.write(self.text)
        if self.reproduce:
            self.copy(self.storedFile, self.activeFile)
        return []
        
    def record(self, *args):
        # Copy the file, as well as the fact it has been stored
        ResponseTraffic.record(self, *args)
        if not self.reproduce:
            self.copy(self.activeFile, self.storedFile)
        
    
class ClientSocketTraffic(Traffic):
    destination = None
    direction = "<-"
    typeId = "CLI"
    def forwardToDestination(self):
        if self.destination:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(self.destination)
            sock.sendall(self.text)
            try:
                sock.shutdown(socket.SHUT_WR)
                response = sock.makefile().read()
                sock.close()
                return [ ServerTraffic(response, self.responseFile) ]
            except socket.error:
                sys.stderr.write("WARNING: Server process reset the connection while TextTest's 'fake client' was trying to read a response from it!\n")
                sys.stderr.write("(while running test at " + CommandLineTraffic.currentTestPath + ")\n")
                sock.close()
        return []


class ServerTraffic(Traffic):
    typeId = "SRV"
    direction = "->"

class ServerStateTraffic(ServerTraffic):
    def __init__(self, inText, responseFile):
        ServerTraffic.__init__(self, inText, responseFile)
        if not ClientSocketTraffic.destination:
            lastWord = inText.strip().split()[-1]
            host, port = lastWord.split(":")
            ClientSocketTraffic.destination = host, int(port)
            # If we get a server state message, switch the order around
            ClientSocketTraffic.direction = "->"
            ServerTraffic.direction = "<-"
    def forwardToDestination(self):
        return []

class PythonInstanceWrapper:
    allInstances = {}
    wrappersByInstance = {}
    def __init__(self, instance, moduleName, classDesc):
        self.instance = instance
        self.moduleName = moduleName
        self.classDesc = classDesc
        self.instanceName = self.getNewInstanceName()
        self.allInstances[self.instanceName] = self
        self.wrappersByInstance[id(self.instance)] = self

    @classmethod
    def getInstance(cls, instanceName):
        return cls.allInstances.get(instanceName, sys.modules.get(instanceName, cls.forceImport(instanceName)))

    @classmethod
    def forceImport(cls, moduleName):
        try:
            exec "import " + moduleName
            return sys.modules.get(moduleName)
        except ImportError:
            pass

    @classmethod
    def getWrapperFor(cls, instance, *args):
        storedWrapper = cls.wrappersByInstance.get(id(instance))
        return storedWrapper or cls(instance, *args)        

    def __repr__(self):
        return "Instance(" + repr(self.classDesc) + ", " + repr(self.instanceName) + ")"

    def getNewInstanceName(self):
        className = self.classDesc.split("(")[0].lower()
        num = 1
        while self.allInstances.has_key(className + str(num)):
            num += 1
        return className + str(num)

    def __getattr__(self, name):
        return getattr(self.instance, name)


class PythonTraffic(Traffic):
    typeId = "PYT"
    direction = "<-"
    def getExceptionResponse(self):
        exc_value = sys.exc_info()[1]
        return PythonResponseTraffic(self.getExceptionText(exc_value), self.responseFile)

    def getExceptionText(self, exc_value):
        return "raise " + exc_value.__class__.__module__ + "." + exc_value.__class__.__name__ + "(" + repr(str(exc_value)) + ")"


class PythonImportTraffic(PythonTraffic):
    def __init__(self, inText, responseFile):
        self.moduleName = inText
        text = "import " + self.moduleName
        super(PythonImportTraffic, self).__init__(text, responseFile)

    def isMarkedForReplay(self, replayItems):
        return self.moduleName in replayItems

    def forwardToDestination(self):
        try:
            exec self.text
            return []
        except:
            return [ self.getExceptionResponse() ]


class PythonModuleTraffic(PythonTraffic):
    interceptModules = set()
    alterations = {}
    @classmethod
    def configure(cls, options):
        modStr = options.python_module_intercepts
        if modStr:
            cls.interceptModules.update(modStr.split(","))
        fullAlterStr = options.alter_response
        if fullAlterStr:
            for alterStr in fullAlterStr.split(","):
                toFind, toReplace = alterStr[:-1].split("{REPLACE ")
                cls.alterations[re.compile(toFind)] = toReplace        

    def __init__(self, modOrObjName, attrName, *args):
        self.modOrObjName = modOrObjName
        self.attrName = attrName
        super(PythonModuleTraffic, self).__init__(*args)

    def getModuleName(self, obj):
        if hasattr(obj, "__module__"): # classes, functions, many instances
            return obj.__module__
        else:
            return obj.__class__.__module__ # many other instances

    def isMarkedForReplay(self, replayItems):
        if PythonInstanceWrapper.getInstance(self.modOrObjName) is None:
            return True
        textMarker = self.modOrObjName + "." + self.attrName
        return any((item == textMarker or textMarker.startswith(item + ".") for item in replayItems))

    def belongsToInterceptedModule(self, moduleName):
        if moduleName in self.interceptModules:
            return True
        elif "." in moduleName:
            return self.belongsToInterceptedModule(moduleName.rsplit(".", 1)[0])
        else:
            return False

    def isBasicType(self, obj):
        return obj is None or obj is NotImplemented or type(obj) in (bool, float, int, long, str, unicode, list, dict, tuple)

    def getPossibleCompositeAttribute(self, instance, attrName):
        attrParts = attrName.split(".", 1)
        firstAttr = getattr(instance, attrParts[0])
        if len(attrParts) == 1:
            return firstAttr
        else:
            return self.getPossibleCompositeAttribute(firstAttr, attrParts[1])

    def evaluate(self, argStr):
        class UnknownInstanceWrapper:
            def __init__(self, name):
                self.instanceName = name
        class NameFinder:
            def __getitem__(self, name):
                return PythonInstanceWrapper.getInstance(name) or UnknownInstanceWrapper(name)
        try:
            return eval(argStr)
        except NameError:
            return eval(argStr, globals(), NameFinder())

    def getResultText(self, result):
        text = repr(self.addInstanceWrappers(result))
        for regex, repl in self.alterations.items():
            text = regex.sub(repl, text)
        return text
    
    def addInstanceWrappers(self, result):
        if type(result) in (list, tuple):
            return type(result)(map(self.addInstanceWrappers, result))
        elif type(result) == dict:
            newResult = {}
            for key, value in result.items():
                newResult[key] = self.addInstanceWrappers(value)
            return newResult
        elif not self.isBasicType(result) and self.belongsToInterceptedModule(self.getModuleName(result)):
            return self.getWrapper(result, self.modOrObjName)
        else:
            return result

    def getWrapper(self, instance, moduleName):
        classDesc = self.getClassDescription(instance.__class__)
        return PythonInstanceWrapper.getWrapperFor(instance, moduleName, classDesc)

    def getClassDescription(self, cls):
        baseClasses = self.findRelevantBaseClasses(cls)
        if len(baseClasses):
            return cls.__name__ + "(" + ", ".join(baseClasses) + ")"
        else:
            return cls.__name__

    def findRelevantBaseClasses(self, cls):
        classes = []
        for baseClass in inspect.getmro(cls)[1:]:
            name = baseClass.__name__
            if self.belongsToInterceptedModule(baseClass.__module__):
                classes.append(name)
            else:
                module = baseClass.__module__
                if module != "__builtin__":
                    name = module + "." + name
                classes.append(name)
                break
        return classes
    

class PythonAttributeTraffic(PythonModuleTraffic):
    cachedAttributes = set()
    def __init__(self, inText, responseFile):
        modOrObjName, attrName = inText.split(":SUT_SEP:")
        text = modOrObjName + "." + attrName
        # Should record these at most once, and only then if they return something in their own right
        # rather than a function etc
        self.foundInCache = text in self.cachedAttributes
        self.cachedAttributes.add(text)
        super(PythonAttributeTraffic, self).__init__(modOrObjName, attrName, text, responseFile)

    def enquiryOnly(self, responses=[]):
        return len(responses) == 0 or self.foundInCache

    def shouldCache(self, obj):
        return type(obj) not in (types.FunctionType, types.GeneratorType, types.MethodType, types.BuiltinFunctionType,
                                 types.ClassType, types.TypeType, types.ModuleType) and \
                                 not hasattr(obj, "__call__")

    def forwardToDestination(self):
        instance = PythonInstanceWrapper.getInstance(self.modOrObjName)
        try:
            attr = self.getPossibleCompositeAttribute(instance, self.attrName)
        except:
            if self.attrName == "__all__":
                # Need to provide something here, the application has probably called 'from module import *'
                attr = filter(lambda x: not x.startswith("__"), dir(instance))
            else:
                return [ self.getExceptionResponse() ]
        if self.shouldCache(attr):
            resultText = self.getResultText(attr)
            return [ PythonResponseTraffic(resultText, self.responseFile) ]
        else:
            # Makes things more readable if we delay evaluating this until the function is called
            # It's rare in Python to cache functions/classes before calling: it's normal to cache other things
            return []
        
        
class PythonSetAttributeTraffic(PythonModuleTraffic):
    def __init__(self, inText, responseFile):
        modOrObjName, attrName, self.valueStr = inText.split(":SUT_SEP:")
        text = modOrObjName + "." + attrName + " = " + self.valueStr
        super(PythonSetAttributeTraffic, self).__init__(modOrObjName, attrName, text, responseFile)

    def forwardToDestination(self):
        instance = PythonInstanceWrapper.getInstance(self.modOrObjName)
        value = self.evaluate(self.valueStr)
        setattr(instance.instance, self.attrName, value)
        return []


class PythonFunctionCallTraffic(PythonModuleTraffic):        
    def __init__(self, inText, responseFile):
        modOrObjName, attrName, argStr, keywDictStr = inText.split(":SUT_SEP:")
        self.args = ()
        self.keyw = {}
        argsForRecord = []
        try:
            internalArgs = self.evaluate(argStr)
            self.args = tuple(map(self.getArgInstance, internalArgs))
            argsForRecord += [ self.getArgForRecord(arg) for arg in internalArgs ]
        except:
            # Not ideal, but better than exit with exception
            # If this happens we probably can't handle the arguments anyway
            # Slightly daring text-munging of Python tuple repr, main thing is to print something vaguely representative
            argsForRecord += argStr.replace(",)", ")")[1:-1].split(", ")
        try:
            internalKw = self.evaluate(keywDictStr)
            for key, value in internalKw.items():
                self.keyw[key] = self.getArgInstance(value)
            for key in sorted(internalKw.keys()):
                value = internalKw[key]
                argsForRecord.append(key + "=" + self.getArgForRecord(value))
        except:
            # Not ideal, but better than exit with exception
            # If this happens we probably can't handle the keyword objects anyway
            # Slightly daring text-munging of Python dictionary repr, main thing is to print something vaguely representative
            argsForRecord += keywDictStr.replace("': ", "=").replace(", '", ", ")[2:-1].split(", ")
        text = modOrObjName + "." + attrName + "(" + ", ".join(argsForRecord) + ")"
        super(PythonFunctionCallTraffic, self).__init__(modOrObjName, attrName, text, responseFile)

    def getArgForRecord(self, arg):
        class ArgWrapper:
            def __init__(self, arg):
                self.arg = arg
            def __repr__(self):
                if hasattr(self.arg, "instanceName"):
                    return self.arg.instanceName
                elif isinstance(self.arg, list):
                    return repr([ ArgWrapper(subarg) for subarg in self.arg ])
                elif isinstance(self.arg, dict):
                    newDict = {}
                    for key, val in self.arg.items():
                        newDict[key] = ArgWrapper(val)
                    return repr(newDict)
                elif isinstance(self.arg, float):
                    # Stick to 2 dp for recording floating point values
                    return str(round(self.arg, 2))
                else:
                    out = repr(self.arg)
                    # Replace linebreaks but don't mangle e.g. Windows paths
                    # This won't work if both exist in the same string - fixing that requires
                    # using a regex and I couldn't make it work [gjb 100922]
                    if "\\n" in out and "\\\\n" not in out: 
                        pos = out.find("'", 0, 2)
                        if pos != -1:
                            return out[:pos] + "''" + out[pos:].replace("\\n", "\n") + "''"
                        else:
                            pos = out.find('"', 0, 2)
                            return out[:pos] + '""' + out[pos:].replace("\\n", "\n") + '""'
                    else:
                        return out
        return repr(ArgWrapper(arg))
            
    def getArgInstance(self, arg):
        if isinstance(arg, PythonInstanceWrapper):
            return arg.instance
        elif isinstance(arg, list):
            return map(self.getArgInstance, arg)
        else:
            return arg

    def callFunction(self, instance):
        if self.attrName == "__repr__" and isinstance(instance, PythonInstanceWrapper): # Has to be special case as we use it internally
            return repr(instance.instance)
        else:
            func = self.getPossibleCompositeAttribute(instance, self.attrName)
            return func(*self.args, **self.keyw)
    
    def getResult(self):
        instance = PythonInstanceWrapper.getInstance(self.modOrObjName)
        try:
            result = self.callFunction(instance)
            return self.getResultText(result)
        except:
            exc_value = sys.exc_info()[1]
            moduleName = self.getModuleName(exc_value)
            if self.belongsToInterceptedModule(moduleName):
                # We own the exception object also, handle it like an ordinary instance
                wrapper = self.getWrapper(exc_value, moduleName)
                return "raise " + repr(wrapper)
            else:
                return self.getExceptionText(exc_value)

    def forwardToDestination(self):
        result = self.getResult()
        if result != "None":
            return [ PythonResponseTraffic(result, self.responseFile) ]
        else:
            return []


class PythonResponseTraffic(ResponseTraffic):
    typeId = "RET"


# Only works on UNIX
class CommandLineKillTraffic(Traffic):
    pidMap = {}
    def __init__(self, inText, responseFile):
        killStr, proxyPid = inText.split(":SUT_SEP:")
        self.killSignal = int(killStr)
        self.proc = self.pidMap.get(proxyPid)
        Traffic.__init__(self, killStr, responseFile)
            
    def forwardToDestination(self):
        if self.proc:
            self.proc.send_signal(self.killSignal)
        return []

    def hasInfo(self):
        return False # We can get these during replay, but should ignore them

    def record(self, *args):
        pass # We replay these entirely from the return code, so that replay works on Windows

class CommandLineTraffic(Traffic):
    typeId = "CMD"
    direction = "<-"
    currentTestPath = None
    environmentDict = {}
    asynchronousFileEditCmds = []
    realCommands = {}
    @classmethod
    def configure(cls, options):
        cls.currentTestPath = options.test_path
        cls.environmentDict = parseCmdDictionary(options.transfer_environment, listvals=True)
        if options.asynchronous_file_edit_commands:
            cls.asynchronousFileEditCmds = options.asynchronous_file_edit_commands.split(",")
        
    def __init__(self, inText, responseFile):
        self.diag = logging.getLogger("Traffic Server")
        cmdText, environText, cmdCwd, proxyPid = inText.split(":SUT_SEP:")
        argv = eval(cmdText)
        self.cmdEnviron = eval(environText)
        self.cmdCwd = cmdCwd
        self.proxyPid = proxyPid
        self.diag.info("Received command with cwd = " + cmdCwd)
        self.fullCommand = argv[0].replace("\\", "/")
        self.commandName = os.path.basename(self.fullCommand)
        self.cmdArgs = [ self.commandName ] + argv[1:]
        envVarsSet, envVarsUnset = self.filterEnvironment(self.cmdEnviron)
        cmdString = " ".join(map(self.quoteArg, self.cmdArgs))
        text = self.getEnvString(envVarsSet, envVarsUnset) + cmdString
        super(CommandLineTraffic, self).__init__(text, responseFile)
        
    def filterEnvironment(self, cmdEnviron):
        envVarsSet, envVarsUnset = [], []
        for var in self.getEnvironmentVariables():
            value = cmdEnviron.get(var)
            currValue = os.getenv(var)
            self.diag.info("Checking environment " + var + "=" + repr(value) + " against " + repr(currValue))
            if value != currValue:
                if value is None:
                    envVarsUnset.append(var)
                else:
                    envVarsSet.append((var, value))
        return envVarsSet, envVarsUnset

    def isMarkedForReplay(self, replayItems):
        return self.commandName in replayItems

    def getEnvironmentVariables(self):
        return self.environmentDict.get(self.commandName, []) + \
               self.environmentDict.get("default", [])

    def hasChangedWorkingDirectory(self):
        return self.cmdCwd != os.getcwd()

    def quoteArg(self, arg):
        if " " in arg:
            return '"' + arg + '"'
        else:
            return arg

    def getEnvString(self, envVarsSet, envVarsUnset):
        recStr = ""
        if self.hasChangedWorkingDirectory():
            recStr += "cd " + self.cmdCwd.replace("\\", "/") + "; "
        if len(envVarsSet) == 0 and len(envVarsUnset) == 0:
            return recStr
        recStr += "env "
        for var in envVarsUnset:
            recStr += "--unset=" + var + " "
        for var, value in envVarsSet:
            recStr += "'" + var + "=" + self.getEnvValueString(var, value) + "' "
        return recStr

    def getEnvValueString(self, var, value):
        oldVal = os.getenv(var)
        if oldVal and oldVal != value:            
            return value.replace(oldVal, "$" + var)
        else:
            return value
        
    def findPossibleFileEdits(self):
        edits = []
        changedCwd = self.hasChangedWorkingDirectory()
        if changedCwd:
            edits.append(self.cmdCwd)
        for arg in self.cmdArgs[1:]:
            for word in self.getFileWordsFromArg(arg):
                if os.path.isabs(word):
                    edits.append(word)
                elif not changedCwd:
                    fullPath = os.path.join(self.cmdCwd, word)
                    if os.path.exists(fullPath):
                        edits.append(fullPath)
        self.removeSubPaths(edits) # don't want to in effect mark the same file twice
        self.diag.info("Might edit in " + repr(edits))
        return edits

    def makesAsynchronousEdits(self):
        return self.commandName in self.asynchronousFileEditCmds
    
    @staticmethod
    def removeSubPaths(paths):
        subPaths = []
        realPaths = map(os.path.realpath, paths)
        for index, path1 in enumerate(realPaths):
            for path2 in realPaths:
                if path1 != path2 and path1.startswith(path2):
                    subPaths.append(paths[index])
                    break

        for path in subPaths:
            paths.remove(path)

    @staticmethod
    def getFileWordsFromArg(arg):
        if arg.startswith("-"):
            # look for something of the kind --logfile=/path
            return arg.split("=")[1:]
        else:
            # otherwise assume we could have multiple words in quotes
            return arg.split()
        
    def forwardToDestination(self):
        try:
            self.diag.info("Running real command with args : " + repr(self.cmdArgs))
            proc = subprocess.Popen(self.cmdArgs, env=self.cmdEnviron, cwd=self.cmdCwd, 
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            CommandLineKillTraffic.pidMap[self.proxyPid] = proc
            output, errors = proc.communicate()
            response = self.makeResponse(output, errors, proc.returncode)
            del CommandLineKillTraffic.pidMap[self.proxyPid]
            return response
        except OSError:
            return self.makeResponse("", "ERROR: Traffic server could not find command '" + self.commandName + "' in PATH\n", 1)

    def makeResponse(self, output, errors, exitCode):
        return [ StdoutTraffic(output, self.responseFile), StderrTraffic(errors, self.responseFile), \
                 SysExitTraffic(exitCode, self.responseFile) ]
    
    def filterReplay(self, trafficList):
        insertIndex = 0
        while len(trafficList) > insertIndex and isinstance(trafficList[insertIndex], FileEditTraffic):
            insertIndex += 1
        
        if len(trafficList) == insertIndex or not isinstance(trafficList[insertIndex], StdoutTraffic):
            trafficList.insert(insertIndex, StdoutTraffic("", self.responseFile))

        insertIndex += 1
        if len(trafficList) == insertIndex or not isinstance(trafficList[insertIndex], StderrTraffic):
            trafficList.insert(insertIndex, StderrTraffic("", self.responseFile))

        insertIndex += 1
        if len(trafficList) == insertIndex or not isinstance(trafficList[insertIndex], SysExitTraffic):
            trafficList.insert(insertIndex, SysExitTraffic("0", self.responseFile))

        return trafficList
    

class TrafficRequestHandler(StreamRequestHandler):
    parseDict = { "SUT_SERVER"           : ServerStateTraffic,
                  "SUT_COMMAND_LINE"     : CommandLineTraffic,
                  "SUT_COMMAND_KILL"     : CommandLineKillTraffic,
                  "SUT_PYTHON_CALL"      : PythonFunctionCallTraffic,
                  "SUT_PYTHON_ATTR"      : PythonAttributeTraffic,
                  "SUT_PYTHON_SETATTR"   : PythonSetAttributeTraffic,
                  "SUT_PYTHON_IMPORT"    : PythonImportTraffic }
    def __init__(self, requestNumber, *args):
        self.requestNumber = requestNumber
        StreamRequestHandler.__init__(self, *args)
        
    def handle(self):
        self.server.diag.info("Received incoming request...")
        text = self.rfile.read()
        self.server.diag.info("Request text : " + text)
        if text.startswith("TERMINATE_SERVER"):
            self.server.shutdown()
        else:
            traffic = self.parseTraffic(text)
            self.server.process(traffic, self.requestNumber)
            self.server.diag.info("Finished processing incoming request")

    def parseTraffic(self, text):
        for key in self.parseDict.keys():
            prefix = key + ":"
            if text.startswith(prefix):
                value = text[len(prefix):]
                return self.parseDict[key](value, self.wfile)
        return ClientSocketTraffic(text, self.wfile)

        
# The basic point here is to make sure that traffic appears in the record
# file in the order in which it comes in, not in the order in which it completes (which is indeterministic and
# may be wrong next time around)
class RecordFileHandler:
    def __init__(self, file):
        self.file = file
        self.recordingRequest = 1
        self.cache = {}
        self.completedRequests = []
        self.lock = threading.Lock()

    def requestComplete(self, requestNumber):
        self.lock.acquire()
        if requestNumber == self.recordingRequest:
            self.recordingRequestComplete()
        else:
            self.completedRequests.append(requestNumber)
        self.lock.release()

    def writeFromCache(self):
        text = self.cache.get(self.recordingRequest)
        if text:
            self.doRecord(text)
            del self.cache[self.recordingRequest]
            
    def recordingRequestComplete(self):
        self.writeFromCache()
        self.recordingRequest += 1
        if self.recordingRequest in self.completedRequests:
            self.recordingRequestComplete()

    def record(self, text, requestNumber):
        self.lock.acquire()
        if requestNumber == self.recordingRequest:
            self.writeFromCache()
            self.doRecord(text)
        else:
            self.cache.setdefault(requestNumber, "")
            self.cache[requestNumber] += text
        self.lock.release()

    def doRecord(self, text):
        writeFile = open(self.file, "a")
        writeFile.write(text)
        writeFile.flush()
        writeFile.close()


class ReplayInfo:
    def __init__(self, replayFile, replayItemString):
        self.responseMap = OrderedDict()
        self.diag = logging.getLogger("Traffic Replay")
        if replayFile:
            self.readReplayFile(replayFile)
        self.replayItems = []
        if replayItemString:
            self.replayItems = replayItemString.split(",")

    def isActiveForAll(self):
        return len(self.responseMap) > 0 and len(self.replayItems) == 0
            
    def isActiveFor(self, traffic):
        if len(self.responseMap) == 0:
            return False
        elif len(self.replayItems) == 0:
            return True
        else:
            return traffic.isMarkedForReplay(set(self.replayItems))

    def readReplayFile(self, replayFile):
        trafficList = self.readIntoList(replayFile)
        currResponseHandler = None
        for trafficStr in trafficList:
            if trafficStr.startswith("<-"):
                currTrafficIn = trafficStr.strip()
                currResponseHandler = self.responseMap.get(currTrafficIn)
                if currResponseHandler:
                    currResponseHandler.newResponse()
                else:
                    currResponseHandler = ReplayedResponseHandler()
                    self.responseMap[currTrafficIn] = currResponseHandler
            elif currResponseHandler:
                currResponseHandler.addResponse(trafficStr)
        self.diag.info("Replay info " + repr(self.responseMap))

    def readIntoList(self, replayFile):
        trafficList = []
        currTraffic = ""
        for line in open(replayFile, "rU").xreadlines():
            if line.startswith("<-") or line.startswith("->"):
                if currTraffic:
                    trafficList.append(currTraffic)
                currTraffic = ""
            currTraffic += line
        if currTraffic:
            trafficList.append(currTraffic)
        return trafficList
    
    def readReplayResponses(self, traffic):
        # We return the response matching the traffic in if we can, otherwise
        # the one that is most similar to it
        if not traffic.hasInfo():
            return []

        responseMapKey = self.getResponseMapKey(traffic)
        if responseMapKey:
            return self.responseMap[responseMapKey].makeResponses(traffic)
        else:
            return []

    def getResponseMapKey(self, traffic):
        desc = traffic.getDescription()
        self.diag.info("Trying to match '" + desc + "'")
        if self.responseMap.has_key(desc):
            self.diag.info("Found exact match")
            return desc
        elif not traffic.enquiryOnly():
            return self.findBestMatch(desc)

    def findBestMatch(self, desc):
        descWords = self.getWords(desc)
        bestMatch = None
        bestMatchInfo = set(), 100000
        for currDesc, responseHandler in self.responseMap.items():
            if self.sameType(desc, currDesc):
                descToCompare = currDesc                    
                self.diag.info("Comparing with '" + descToCompare + "'")
                matchInfo = self.getWords(descToCompare), responseHandler.getUnmatchedResponseCount()
                if self.isBetterMatch(matchInfo, bestMatchInfo, descWords):
                    bestMatchInfo = matchInfo
                    bestMatch = currDesc

        if bestMatch is not None:
            self.diag.info("Best match chosen as '" + bestMatch + "'")
            return bestMatch

    def sameType(self, desc1, desc2):
        return desc1[2:5] == desc2[2:5]

    def getWords(self, desc):
        # Heuristic decisions trying to make the best of inexact matches
        separators = [ "/", "(", ")", "\\", None ] # the last means whitespace...
        return self._getWords(desc, separators)

    def _getWords(self, desc, separators):
        if len(separators) == 0:
            return [ desc ]
        
        words = []
        for part in desc.split(separators[0]):
            words += self._getWords(part, separators[1:])
        return words

    def getMatchingBlocks(self, list1, list2):
        matcher = difflib.SequenceMatcher(None, list1, list2)
        return matcher.get_matching_blocks()

    def commonElementCount(self, blocks):
        return sum((block.size for block in blocks))

    def nonMatchingSequenceCount(self, blocks):
        if len(blocks) > 1 and self.lastBlockReachesEnd(blocks):
            return len(blocks) - 2
        else:
            return len(blocks) - 1

    def lastBlockReachesEnd(self, blocks):
        return blocks[-2].a + blocks[-2].size == blocks[-1].a and \
               blocks[-2].b + blocks[-2].size == blocks[-1].b
            
    def isBetterMatch(self, info1, info2, targetWords):
        words1, unmatchedCount1 = info1
        words2, unmatchedCount2 = info2
        blocks1 = self.getMatchingBlocks(words1, targetWords)
        blocks2 = self.getMatchingBlocks(words2, targetWords)
        common1 = self.commonElementCount(blocks1)
        common2 = self.commonElementCount(blocks2)
        self.diag.info("Words in common " + repr(common1) + " vs " + repr(common2))
        if common1 > common2:
            return True
        elif common1 < common2:
            return False

        nonMatchCount1 = self.nonMatchingSequenceCount(blocks1)
        nonMatchCount2 = self.nonMatchingSequenceCount(blocks2)
        self.diag.info("Non matching sequences " + repr(nonMatchCount1) + " vs " + repr(nonMatchCount2))
        if nonMatchCount1 < nonMatchCount2:
            return True
        elif nonMatchCount1 > nonMatchCount2:
            return False

        self.diag.info("Unmatched count difference " + repr(unmatchedCount1) + " vs " + repr(unmatchedCount2))
        return unmatchedCount1 > unmatchedCount2
    

# Need to handle multiple replies to the same question
class ReplayedResponseHandler:
    def __init__(self):
        self.timesChosen = 0
        self.responses = [[]]
    def __repr__(self):
        return repr(self.responses)
    def newResponse(self):
        self.responses.append([])        
    def addResponse(self, trafficStr):
        self.responses[-1].append(trafficStr)
    def getCurrentStrings(self):
        if self.timesChosen < len(self.responses):
            currStrings = self.responses[self.timesChosen]
        else:
            currStrings = self.responses[0]
        return currStrings

    def getUnmatchedResponseCount(self):
        return len(self.responses) - self.timesChosen
    
    def makeResponses(self, traffic):
        trafficStrings = self.getCurrentStrings()
        responses = []
        for trafficStr in trafficStrings:
            trafficType = trafficStr[2:5]
            allClasses = [ FileEditTraffic, ClientSocketTraffic, ServerTraffic,
                           StdoutTraffic, StderrTraffic, SysExitTraffic, PythonResponseTraffic ]
            for trafficClass in allClasses:
                if trafficClass.typeId == trafficType:
                    responses.append((trafficClass, trafficStr[6:]))
        self.timesChosen += 1
        return responses
        
        
def main():
    parser = create_option_parser()
    options = parser.parse_args()[0] # no positional arguments
    logging.config.fileConfig(options.logconfigfile, parseCmdDictionary(options.logdefaults, listvals=False))

    for cls in [ CommandLineTraffic, FileEditTraffic, PythonModuleTraffic ]:
        cls.configure(options)

    server = TrafficServer(options)
    server.run()