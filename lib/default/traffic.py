
import os, stat, sys, plugins, logging, shutil, socket, subprocess

class SetUpTrafficHandlers(plugins.Action):
    def __init__(self, record):
        self.record = record
        self.trafficServerProcess = None
        self.trafficFiles = self.findTrafficFiles()
        self.trafficPyModuleFile = os.path.join(plugins.installationDir("libexec"), "traffic_pymodule.py")
        self.trafficServerFile = os.path.join(plugins.installationDir("libexec"), "traffic_server.py")
        
    def findTrafficFiles(self):
        libExecDir = plugins.installationDir("libexec") 
        files = [ os.path.join(libExecDir, "traffic_cmd.py") ]
        if os.name == "nt":
            files.append(os.path.join(libExecDir, "traffic_cmd.exe"))
        return files

    def terminateServer(self, test):
        servAddr = test.getEnvironment("TEXTTEST_MIM_SERVER")
        if servAddr:
            sendSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            host, port = servAddr.split(":")
            serverAddress = (host, int(port))
            try:
                sendSocket.connect(serverAddress)
                sendSocket.sendall("TERMINATE_SERVER\n")
                sendSocket.shutdown(2)
            except socket.error:
                print "Could not send terminate message to traffic server, seemed not to be running anyway."
        if self.trafficServerProcess:
            out, err = self.trafficServerProcess.communicate()
            if err:
                sys.stderr.write("Error from Traffic Server :\n" + err)
            self.trafficServerProcess = None

    def __call__(self, test):
        if self.trafficServerProcess:
            # After the test is complete we shut down the traffic server and allow it to flush itself
            self.terminateServer(test)
        else:
            replayFile = test.getFileName("traffic")
            if self.record or replayFile:
                self.makeIntercepts(test)
                self.trafficServerProcess = self.makeTrafficServer(test, replayFile)

    def makeArgFromDict(self, dict):
        args = [ key + "=" + self.makeArgFromVal(val) for key, val in dict.items() ]
        return ",".join(args)

    def makeArgFromVal(self, val):
        if type(val) == str:
            return val
        else:
            return "+".join(val)
            
    def makeTrafficServer(self, test, replayFile):
        recordFile = test.makeTmpFileName("traffic")
        recordEditDir = test.makeTmpFileName("file_edits", forComparison=0)
        cmdArgs = [ sys.executable, self.trafficServerFile, "-t", test.getRelPath(), "-r", recordFile, "-F", recordEditDir ]
        if not self.record:
            cmdArgs += [ "-p", replayFile ]
            replayEditDir = test.getFileName("file_edits")
            if replayEditDir:
                cmdArgs += [ "-f", replayEditDir ]

        if test.getConfigValue("collect_traffic_use_threads") != "true":
            cmdArgs += [ "-s" ]
            
        filesToIgnore = test.getCompositeConfigValue("test_data_ignore", "file_edits")
        if filesToIgnore:
            cmdArgs += [ "-i", ",".join(filesToIgnore) ]

        environmentDict = test.getConfigValue("collect_traffic_environment")
        if environmentDict:
            cmdArgs += [ "-e", self.makeArgFromDict(environmentDict) ]

        pythonModules = test.getConfigValue("collect_traffic_py_module")
        if pythonModules:
            cmdArgs += [ "-x", ",".join(pythonModules) ]

        asynchronousFileEditCmds = test.getConfigValue("collect_traffic").get("asynchronous")
        if asynchronousFileEditCmds:
            cmdArgs += [ "-a", ",".join(asynchronousFileEditCmds) ]

        proc = subprocess.Popen(cmdArgs, env=test.getRunEnvironment(), universal_newlines=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        address = proc.stdout.readline().strip()
        test.setEnvironment("TEXTTEST_MIM_SERVER", address) # Address of TextTest's server for recording client/server traffic
        if len(pythonModules):
            # Change test environment to pick up the intercepts
            test.setEnvironment("PYTHONPATH", test.getDirectory(temporary=1) + os.pathsep + test.getEnvironment("PYTHONPATH", ""))
        return proc
                    
    def makeIntercepts(self, test):
        for cmd in self.getCommandsForInterception(test):
            self.intercept(test, cmd, self.trafficFiles, copyExtension=True)

        for moduleName in test.getConfigValue("collect_traffic_py_module"):
            modulePath = moduleName.replace(".", "/")
            self.intercept(test, modulePath + ".py", [ self.trafficPyModuleFile ], copyExtension=False)
            self.makePackageFiles(test, modulePath)
     
    def makePackageFiles(self, test, modulePath):
        parts = modulePath.rsplit("/", 1)
        if len(parts) == 2:
            localFileName = os.path.join(parts[0], "__init__.py")
            fileName = test.makeTmpFileName(localFileName, forComparison=0)
            open(fileName, "w").close() # make an empty package file
            self.makePackageFiles(test, parts[0])

    def getCommandsForInterception(self, test):
        # This gets all names in collect_traffic, not just those marked
        # "asynchronous"! (it will also pick up "default").
        return test.getCompositeConfigValue("collect_traffic", "asynchronous")

    def intercept(self, test, cmd, trafficFiles, copyExtension):
        interceptName = test.makeTmpFileName(cmd, forComparison=0)
        plugins.ensureDirExistsForFile(interceptName)
        if os.path.exists(interceptName):
            return sys.stderr.write("Could not create interceptor file '" + os.path.basename(interceptName) + "' - file already existed for other purposes.\n") 
        for trafficFile in trafficFiles:
            if os.name == "posix":
                os.symlink(trafficFile, interceptName)
            elif copyExtension:
                # Rename the files as appropriate and hope for the best :)
                extension = os.path.splitext(trafficFile)[-1]
                shutil.copy(trafficFile, interceptName + extension)
            else:
                shutil.copy(trafficFile, interceptName)


class ModifyTraffic(plugins.ScriptWithArgs):
    # For now, only bother with the client server traffic which is mostly what needs tweaking...
    scriptDoc = "Apply a script to all the client server data"
    def __init__(self, args):
        argDict = self.parseArguments(args, [ "script" ])
        self.script = argDict.get("script")
    def __repr__(self):
        return "Updating traffic in"
    def __call__(self, test):
        try:
            fileName = test.getFileName("traffic")
            if fileName:
                self.describe(test)
                newFileName = fileName + "tmpedit"
                newFile = open(newFileName, "w")
                for trafficText in self.readIntoList(fileName):
                    newTrafficText = self.getModified(trafficText, test.getDirectory())
                    self.write(newFile, newTrafficText) 
                newFile.close()
                shutil.move(newFileName, fileName)
        except plugins.TextTestError, e:
            print e

    def readIntoList(self, fileName):
        # Copied from traffic server ReplayInfo, easier than trying to reuse it
        trafficList = []
        currTraffic = ""
        for line in open(fileName, "rU").xreadlines():
            if line.startswith("<-") or line.startswith("->"):
                if currTraffic:
                    trafficList.append(currTraffic)
                currTraffic = ""
            currTraffic += line
        if currTraffic:
            trafficList.append(currTraffic)
        return trafficList
            
    def getModified(self, fullLine, dir):
        trafficType = fullLine[2:5]
        if trafficType in [ "CLI", "SRV" ]:
            proc = subprocess.Popen([ self.script, fullLine[6:]], cwd=dir,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=os.name=="nt")
            stdout, stderr = proc.communicate()
            if len(stderr) > 0:
                raise plugins.TextTestError, "Couldn't modify traffic :\n " + stderr
            else:
                return fullLine[:6] + stdout
        else:
            return fullLine
            
    def write(self, newFile, desc):
        if not desc.endswith("\n"):
            desc += "\n"
        newFile.write(desc)

    def setUpSuite(self, suite):
        self.describe(suite)
