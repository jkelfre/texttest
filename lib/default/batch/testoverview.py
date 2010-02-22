# Code to generate HTML report of historical information. This report generated
# either via the -coll flag, or via -s 'batch.GenerateHistoricalReport <batchid>'

import os, plugins, time, re, HTMLgen, HTMLcolors, operator, sys, logging
from cPickle import Pickler, Unpickler, UnpicklingError
from ndict import seqdict
from glob import glob
HTMLgen.PRINTECHO = 0

def getWeekDay(tag):
    return plugins.weekdays[time.strptime(tag.split("_")[0], "%d%b%Y")[6]]
    
class ColourFinder:
    def __init__(self, getConfigValue):
        self.getConfigValue = getConfigValue
    def find(self, title):
        colourName = self.getConfigValue("historical_report_colours", title)
        return self.htmlColour(colourName)
    def htmlColour(self, colourName):
        if not colourName.startswith("#"):
            exec "colourName = HTMLcolors." + colourName.upper()
        return colourName

def getDisplayText(tag):
    displayText = "_".join(tag.split("_")[1:])
    if displayText:
        return displayText
    else:
        return tag

class TitleWithDateStamp:
    def __init__(self, title):
        self.title = title + " (generated by " + os.getenv("USER", os.getenv("USERNAME", "unknown")) + " at "
    def __str__(self):
        return self.title + plugins.localtime(format="%d%b%H:%M") + ")"
            

class GenerateWebPages(object):
    def __init__(self, getConfigValue, pageDir, resourceNames,
                 pageTitle, pageSubTitle, pageVersion, extraVersions, descriptionInfo):
        self.pageTitle = pageTitle
        self.pageSubTitle = pageSubTitle
        self.pageVersion = pageVersion
        self.extraVersions = extraVersions
        self.pageDir = pageDir
        self.pagesOverview = seqdict()
        self.pagesDetails = seqdict()
        self.getConfigValue = getConfigValue
        self.resourceNames = resourceNames
        self.descriptionInfo = descriptionInfo
        self.diag = logging.getLogger("GenerateWebPages")

    def makeSelectors(self, subPageNames, tags=[]):
        allSelectors = []
        firstSubPageName = self.getConfigValue("historical_report_subpages", "default")[0]
        for subPageName in subPageNames:
            if subPageName == firstSubPageName:
                suffix = ""
            else:
                suffix = "_" + subPageName.lower()
            allSelectors.append(Selector(subPageName, suffix, self.getConfigValue, tags))
        return allSelectors
    
    def generate(self, repositoryDirs, subPageNames):
        foundMinorVersions = {}
        allMonthSelectors = set()
        latestMonth = None
        for version, repositoryDirInfo in repositoryDirs.items():
            self.diag.info("Generating " + version)
            allFiles, tags = self.findTestStateFilesAndTags(repositoryDirInfo)
            if len(allFiles) > 0:
                selectors = self.makeSelectors(subPageNames, tags)
                monthSelectors = SelectorByMonth.makeInstances(tags)
                allMonthSelectors.update(monthSelectors)
                allSelectors = selectors + list(reversed(monthSelectors))
                # If we already have month pages, we only regenerate the current one
                if len(self.getExistingMonthPages()) == 0:
                    selectors = allSelectors
                else:
                    currLatestMonthSel = monthSelectors[-1]
                    if latestMonth is None or currLatestMonthSel.linkName == latestMonth:
                        selectors.append(monthSelectors[-1])
                        latestMonth = currLatestMonthSel.linkName
                    tags = list(reduce(set.union, (set(selector.selectedTags) for selector in selectors), set()))
                    tags.sort(self.compareTags)

                loggedTests = seqdict()
                categoryHandlers = {}
                for stateFile, repository in allFiles:
                    tag = self.getTagFromFile(stateFile)
                    if len(tags) == 0 or tag in tags:
                        testId, state, extraVersion = self.processTestStateFile(stateFile, repository)
                        loggedTests.setdefault(extraVersion, seqdict()).setdefault(testId, seqdict())[tag] = state
                        categoryHandlers.setdefault(tag, CategoryHandler()).registerInCategory(testId, state, extraVersion)

                versionToShow = self.removePageVersion(version)
                for resourceName in self.resourceNames:
                    hasData = False
                    for sel in selectors:
                        filePath = self.getPageFilePath(sel, resourceName)
                        if self.pagesOverview.has_key(filePath):
                            page = self.pagesOverview[filePath][-1]
                        else:
                            page = self.createPage(resourceName)
                            self.pagesOverview[filePath] = resourceName, page
                            
                        for cellInfo in self.getCellInfoForResource(resourceName):
                            tableHeader = self.getTableHeader(resourceName, cellInfo, version, repositoryDirs)
                            heading = self.getHeading(resourceName, versionToShow)
                            hasData |= self.addTable(page, cellInfo, categoryHandlers, version,
                                                     loggedTests, sel, tableHeader, filePath, heading)
                    if hasData and versionToShow:
                        link = HTMLgen.Href("#" + version, versionToShow)
                        foundMinorVersions.setdefault(resourceName, HTMLgen.Container()).append(link)
                    
                # put them in reverse order, most relevant first
                linkFromDetailsToOverview = [ sel.getLinkInfo(self.pageVersion) for sel in allSelectors ]
                for tag in tags:
                    details = self.pagesDetails.setdefault(tag, TestDetails(tag, self.pageTitle, self.pageSubTitle))
                    details.addVersionSection(version, categoryHandlers[tag], linkFromDetailsToOverview)
                
        selContainer = HTMLgen.Container()
        for sel in self.makeSelectors(subPageNames):
            target, linkName = sel.getLinkInfo(self.pageVersion)
            selContainer.append(HTMLgen.Href(target, linkName))

        monthContainer = HTMLgen.Container()
        for sel in sorted(allMonthSelectors):
            target, linkName = sel.getLinkInfo(self.pageVersion)
            monthContainer.append(HTMLgen.Href(target, linkName))
            
        for resourceName, page in self.pagesOverview.values():
            if len(monthContainer.contents) > 0:
                page.prepend(HTMLgen.Heading(2, monthContainer, align = 'center'))
            page.prepend(HTMLgen.Heading(2, selContainer, align = 'center'))
            minorVersionHeader = foundMinorVersions.get(resourceName)
            if minorVersionHeader:
                page.prepend(HTMLgen.Heading(1, minorVersionHeader, align = 'center'))
            page.prepend(HTMLgen.Heading(1, self.getHeading(resourceName), align = 'center'))

        self.writePages()

    def getHeading(self, resourceName, versionToShow=""):
        heading = self.getResultType(resourceName) + " results for " + self.pageTitle
        if versionToShow:
            heading += "." + versionToShow
        return heading
    
    def getTableHeader(self, resourceName, cellInfo, version, repositoryDirs):
        parts = []
        if resourceName != cellInfo:
            parts.append(cellInfo.capitalize() + " Results")
        if len(repositoryDirs) > 1:
            parts.append(version)
        return " for ".join(parts)
        
    def getCellInfoForResource(self, resourceName):
        fromConfig = self.getConfigValue("historical_report_resource_page_tables", resourceName)
        if fromConfig:
            return fromConfig
        else:
            return [ resourceName ]

    def getResultType(self, resourceName):
        if resourceName:
            return resourceName.capitalize()
        else:
            return "Test"
        
    def getExistingMonthPages(self):
        return glob(os.path.join(self.pageDir, "test_" + self.pageVersion + "_all_???[0-9][0-9][0-9][0-9].html"))

    def compareTags(self, x, y):
        timeCmp = cmp(self.getTagTimeInSeconds(x), self.getTagTimeInSeconds(y))
        if timeCmp:
            return timeCmp
        else:
            return cmp(x, y) # If the timing is the same, sort alphabetically

    def getTagFromFile(self, fileName):
        return os.path.basename(fileName).replace("teststate_", "")
        
    def findTestStateFilesAndTags(self, repositoryDirs):
        allFiles = []
        allTags = set()
        for extraVersion, dir in repositoryDirs:
            for root, dirs, files in os.walk(dir):
                for file in files:
                    if file.startswith("teststate_"):
                        allFiles.append((os.path.join(root, file), dir))
                        allTags.add(self.getTagFromFile(file))
                                
        return allFiles, sorted(allTags, self.compareTags)
                          
    def processTestStateFile(self, stateFile, repository):
        state = self.readState(stateFile)
        testId = self.getTestIdentifier(stateFile, repository)
        extraVersion = self.findExtraVersion(repository)
        return testId, state, extraVersion
    
    def findExtraVersion(self, repository):
        versions = os.path.basename(repository).split(".")
        for i in xrange(len(versions)):
            version = ".".join(versions[i:])
            if version in self.extraVersions:
                return version
        return ""

    def findGlobal(self, modName, className):
        try:
            exec "from " + modName + " import " + className + " as _class"
            return _class
        except ImportError:
            for loadedMod in sys.modules.keys():
                if "." in loadedMod:
                    packageName = ".".join(loadedMod.split(".")[:-1] + [ modName ])
                    try:
                        exec "from " + packageName + " import " + className + " as _class" 
                        return _class
                    except ImportError:
                        pass
            raise
        
    def getNewState(self, file):
        # Would like to do load(file) here... but it doesn't work with universal line endings, see Python bug 1724366
        from cStringIO import StringIO
        unpickler = Unpickler(StringIO(file.read()))
        # Magic to keep us backward compatible in the face of packages changing...
        unpickler.find_global = self.findGlobal
        return unpickler.load()
        
    def readState(self, stateFile):
        file = open(stateFile, "rU")
        try:
            state = self.getNewState(file)
            if isinstance(state, plugins.TestState):
                return state
            else:
                return self.readErrorState("Incorrect type for state object.")
        except (UnpicklingError, ImportError, EOFError, AttributeError), e:
            if os.path.getsize(stateFile) > 0:
                return self.readErrorState("Stack info follows:\n" + str(e))
            else:
                return plugins.Unrunnable("Results file was empty, probably the disk it resides on is full.", "Disk full?")

    def readErrorState(self, errMsg):
        freeText = "Failed to read results file, possibly deprecated format. " + errMsg
        return plugins.Unrunnable(freeText, "read error")

    def removePageVersion(self, version):
        leftVersions = []
        pageSubVersions = self.pageVersion.split(".")
        for subVersion in version.split("."):
            if not subVersion in pageSubVersions:
                leftVersions.append(subVersion)
        return ".".join(leftVersions)

    def getPageFilePath(self, selector, resourceName):
        pageName = selector.getLinkInfo(self.pageVersion)[0]
        return os.path.join(self.pageDir, resourceName, pageName)
        
    def createPage(self, resourceName):
        style = "body,td {color: #000000;font-size: 11px;font-family: Helvetica;} th {color: #000000;font-size: 13px;font-family: Helvetica;}"
        title = TitleWithDateStamp(self.getResultType(resourceName) + " results for " + self.pageTitle)
        return HTMLgen.SimpleDocument(title=title, style=style)

    def makeTableHeaderCell(self, tableHeader):
        container = HTMLgen.Container()
        container.append(HTMLgen.Name(tableHeader))
        container.append(HTMLgen.U(HTMLgen.Heading(1, tableHeader, align = 'center')))
        return HTMLgen.TD(container)

    def makeImageLink(self, graphFile):
        image = HTMLgen.Image(filename=graphFile, src=graphFile, height=100, width=150, border=0)
        link = HTMLgen.Href(graphFile, image)
        return HTMLgen.TD(link)
        
    def addTable(self, page, cellInfo, categoryHandlers, version, loggedTests, selector, tableHeader, filePath, graphHeading):
        testTable = TestTable(self.getConfigValue, cellInfo, self.descriptionInfo,
                              selector.selectedTags, categoryHandlers, self.pageVersion, version)
        table = testTable.generate(loggedTests)
        if table:
            cells = []
            if tableHeader:
                page.append(HTMLgen.HR())
                cells.append(self.makeTableHeaderCell(tableHeader))

            if not cellInfo: 
                dirname, fileRef = self.getGraphFileParts(filePath, version)
                fullPath = os.path.abspath(os.path.join(dirname, fileRef)).replace("\\", "/")
                if testTable.generateGraph(fullPath, graphHeading):
                    cells.append(self.makeImageLink(fileRef))

            if len(cells):
                row = HTMLgen.TR(*cells)
                initialTable = HTMLgen.TableLite(align="center")
                initialTable.append(row)
                page.append(initialTable)

            extraVersions = loggedTests.keys()[1:]
            if len(extraVersions) > 0:
                page.append(testTable.generateExtraVersionLinks(extraVersions))


            page.append(table)
            return True
        else:
            return False

    def getGraphFileParts(self, filePath, version):
        dirname, local = os.path.split(filePath)
        versionSuffix = self.removePageVersion(version)
        if versionSuffix:
            versionSuffix = "." + versionSuffix
        return dirname, os.path.join("images", local[:-5] + versionSuffix + ".png")
        
    def writePages(self):
        plugins.log.info("Writing overview pages...")
        for pageFile, (resourceName, page) in self.pagesOverview.items():
            page.write(pageFile)
            plugins.log.info("wrote: '" + plugins.relpath(pageFile, self.pageDir) + "'")
        plugins.log.info("Writing detail pages...")
        for resourceName in self.resourceNames:
            for tag, details in self.pagesDetails.items():
                pageName = getDetailPageName(self.pageVersion, tag)
                relPath = os.path.join(resourceName, pageName)
                details.write(os.path.join(self.pageDir, relPath))
                plugins.log.info("wrote: '" + relPath + "'")

    def getTestIdentifier(self, stateFile, repository):
        dir = os.path.dirname(stateFile)
        return dir.replace(repository + os.sep, "").replace(os.sep, " ")

    def getTagTimeInSeconds(self, tag):
        timePart = tag.split("_")[0]
        return time.mktime(time.strptime(timePart, "%d%b%Y"))


class TestTable:
    def __init__(self, getConfigValue, cellInfo, descriptionInfo, tags, categoryHandlers, pageVersion, version):
        self.getConfigValue = getConfigValue
        self.cellInfo = cellInfo
        self.colourFinder = ColourFinder(getConfigValue)
        self.descriptionInfo = descriptionInfo
        self.tags = tags
        self.categoryHandlers = categoryHandlers
        self.pageVersion = pageVersion
        self.version = version

    def generateGraph(self, fileName, heading):
        if len(self.tags) > 1: # Don't bother with graphs when tests have only run once
            try:
                from resultgraphs import GraphGenerator
            except Exception, e:
                sys.stderr.write("Not producing result graphs: " + str(e) + "\n")
                return False # if matplotlib isn't installed or is too old
        
            data = self.getColourKeySummaryData()
            generator = GraphGenerator()
            generator.generateGraph(fileName, heading, data, self.colourFinder)
            return True
        else:
            return False

    def generate(self, loggedTests):
        table = HTMLgen.TableLite(border=0, cellpadding=4, cellspacing=2,width="100%")
        table.append(self.generateTableHead())
        table.append(self.generateSummaries())
        hasRows = False
        for extraVersion, testInfo in loggedTests.items():
            currRows = []
            for test in sorted(testInfo.keys()):
                results = testInfo[test]
                row = self.generateTestRow(test, extraVersion, results)
                if row:
                    currRows.append(row)

            if len(currRows) == 0:
                continue
            else:
                hasRows = True
            
            # Add an extra line in the table only if there are several versions.
            if len(loggedTests) > 1:
                fullVersion = self.version
                if extraVersion:
                    fullVersion += "." + extraVersion
                table.append(self.generateExtraVersionHeader(fullVersion))
                table.append(self.generateSummaries(extraVersion))

            for row in currRows:
                table.append(row)

        if hasRows:
            table.append(HTMLgen.BR())
            return table

    def generateSummaries(self, extraVersion=None):
        bgColour = self.colourFinder.find("column_header_bg")
        row = [ HTMLgen.TD("Summary", bgcolor = bgColour) ]
        for tag in self.tags:
            categoryHandler = self.categoryHandlers[tag]
            detailPageName = getDetailPageName(self.pageVersion, tag)
            summary = categoryHandler.generateHTMLSummary(detailPageName + "#" + self.version, extraVersion)
            row.append(HTMLgen.TD(summary, bgcolor = bgColour))
        return HTMLgen.TR(*row)

    def getColourKeySummaryData(self):
        fullData = []
        for tag in self.tags:
            colourCount = seqdict()
            for colourKey in [ "success", "knownbug", "performance", "memory", "failure" ]:
                colourCount[colourKey] = 0
            categoryHandler = self.categoryHandlers[tag]
            basicData = categoryHandler.getSummaryData()[-1]
            for category, count in basicData.items():
                colourKey = self.getBackgroundColourKey(category)
                colourCount[colourKey] += count
            fullData.append((getDisplayText(tag), colourCount))
        return fullData

    def generateExtraVersionLinks(self, extraVersions):
        cont = HTMLgen.Container()
        for extra in extraVersions:
            fullName = self.version + "." + extra
            cont.append(HTMLgen.Href("#" + fullName, extra))
        return HTMLgen.Heading(2, cont, align='center')
        
    def generateExtraVersionHeader(self, extraVersion):
        bgColour = self.colourFinder.find("column_header_bg")
        extraVersionElement = HTMLgen.Container(HTMLgen.Name(extraVersion), extraVersion)
        columnHeader = HTMLgen.TH(extraVersionElement, colspan = len(self.tags) + 1, bgcolor=bgColour)
        return HTMLgen.TR(columnHeader)
    
    def generateTestRow(self, testName, extraVersion, results):
        bgColour = self.colourFinder.find("row_header_bg")
        testId = self.version + testName + extraVersion
        container = HTMLgen.Container(HTMLgen.Name(testId), testName)
        row = [ HTMLgen.TD(container, bgcolor=bgColour, title=self.descriptionInfo.get(testName, "")) ]
        # Don't add empty rows to the table
        foundData = False
        for tag in self.tags:
            cellContent, bgcol, hasData = self.generateTestCell(tag, testName, testId, results)
            row.append(HTMLgen.TD(cellContent, bgcolor = bgcol))
            foundData |= hasData
            
        if foundData:
            return HTMLgen.TR(*row)

    def getCellData(self, state):
        if state:
            if self.cellInfo:
                if hasattr(state, "findComparison"):
                    fileComp, fileCompList = state.findComparison(self.cellInfo, includeSuccess=True)
                    if fileComp:
                        return self.getCellDataFromFileComp(fileComp)
            else:
                return self.getCellDataFromState(state)

        return "N/A", True, self.colourFinder.find("test_default_fg"), self.colourFinder.find("no_results_bg")

    def getCellDataFromState(self, state):
        if hasattr(state, "getMostSevereFileComparison"):
            fileComp = state.getMostSevereFileComparison()
        else:
            fileComp = None
        fgcol, bgcol = self.getColours(state.category, fileComp)
        filteredState = self.filterState(repr(state))
        detail = state.getTypeBreakdown()[1]
        success = state.category == "success"
        return filteredState + detail, success, fgcol, bgcol

    def getCellDataFromFileComp(self, fileComp):
        success = fileComp.hasSucceeded()
        if success:
            category = "success"
        else:
            category = fileComp.getType()
        fgcol, bgcol = self.getColours(category, fileComp)
        text = str(fileComp.getNewPerformance()) + " " + self.getConfigValue("performance_unit", fileComp.stem)
        return text, success, fgcol, bgcol

    def generateTestCell(self, tag, testName, testId, results):
        state = results.get(tag)
        cellText, success, fgcol, bgcol = self.getCellData(state)
        cellContent = HTMLgen.Font(cellText, color=fgcol) 
        if success:
            return cellContent, bgcol, cellText != "N/A"
        else:
            linkTarget = getDetailPageName(self.pageVersion, tag) + "#" + testId
            tooltip = "'" + testName + "' failure for " + getDisplayText(tag)
            return HTMLgen.Href(linkTarget, cellContent, title=tooltip), bgcol, True
    
    def filterState(self, cellContent):
        result = cellContent
        result = re.sub(r'CRASHED.*( on .*)', r'CRASH\1', result)
        result = re.sub('(\w),(\w)', '\\1, \\2', result)
        result = re.sub(':', '', result)
        result = re.sub(' on ', ' ', result)
        result = re.sub('could not be run', '', result)
        result = re.sub('succeeded', 'ok', result)
        result = re.sub('used more memory','', result)
        result = re.sub('used less memory','', result)
        result = re.sub('ran faster','', result)
        result = re.sub('ran slower','', result)
        result = re.sub('faster\([^ ]+\) ','', result)
        result = re.sub('slower\([^ ]+\) ','', result)
        return result

    def getBackgroundColourKey(self, category):
        if category == "success":
            return "success"
        elif category == "bug":
            return "knownbug"
        elif category.startswith("faster") or category.startswith("slower"):
            return "performance"
        elif category == "smaller" or category == "larger":
            return "memory"
        else:
            return "failure"

    def getForegroundColourKey(self, bgcolKey, fileComp):
        if (bgcolKey == "performance" and self.getPercent(fileComp) >= \
            self.getConfigValue("performance_variation_serious_%", "cputime")) or \
            (bgcolKey == "memory" and self.getPercent(fileComp) >= \
             self.getConfigValue("performance_variation_serious_%", "memory")):
            return "performance"
        else:
            return "test_default"

    def getColours(self, category, fileComp):
        bgcolKey = self.getBackgroundColourKey(category)
        fgcolKey = self.getForegroundColourKey(bgcolKey, fileComp)
        return self.colourFinder.find(fgcolKey + "_fg"), self.colourFinder.find(bgcolKey + "_bg")

    def getPercent(self, fileComp):
        return fileComp.perfComparison.percentageChange

    def findTagColour(self, tag):
        return self.colourFinder.find("run_" + getWeekDay(tag) + "_fg")

    def generateTableHead(self):
        head = [ HTMLgen.TH("Test") ]
        for tag in self.tags:
            tagColour = self.findTagColour(tag)
            linkTarget = getDetailPageName(self.pageVersion, tag)
            linkText = HTMLgen.Font(getDisplayText(tag), color=tagColour)
            link = HTMLgen.Href(linkTarget, linkText)
            head.append(HTMLgen.TH(link))
        heading = HTMLgen.TR()
        heading = heading + head
        return heading

        
class TestDetails:
    def __init__(self, tag, pageTitle, pageSubTitle):
        tagText = getDisplayText(tag)
        pageDetailTitle = "Detailed test results for " + pageTitle + ": " + tagText
        self.document = HTMLgen.SimpleDocument(title=TitleWithDateStamp(pageDetailTitle))
        headerText = tagText + " - detailed test results for " + pageTitle
        self.document.append(HTMLgen.Heading(1, headerText, align = 'center'))
        subTitleText = "(To start TextTest for these tests, run '" + pageSubTitle + "')"
        self.document.append(HTMLgen.Center(HTMLgen.Emphasis(subTitleText)))
        self.totalCategoryHandler = CategoryHandler()
        self.versionSections = []
        
    def addVersionSection(self, version, categoryHandler, linkFromDetailsToOverview):
        self.totalCategoryHandler.update(categoryHandler)
        container = HTMLgen.Container()
        container.append(HTMLgen.HR())
        container.append(self.getSummaryHeading(version, categoryHandler))
        for desc, testInfo in categoryHandler.getTestsWithDescriptions():
            fullDescription = self.getFullDescription(testInfo, version, linkFromDetailsToOverview)
            if fullDescription:
                container.append(HTMLgen.Name(version + desc))
                container.append(HTMLgen.Heading(3, "Detailed information for the tests that " + desc + ":"))
                container.append(fullDescription)
        self.versionSections.append(container)

    def getSummaryHeading(self, version, categoryHandler):
        return HTMLgen.Heading(2, version + ": " + categoryHandler.generateTextSummary())

    def write(self, fileName):
        if len(self.versionSections) > 1:
            self.document.append(self.getSummaryHeading("Total", self.totalCategoryHandler))
        for sect in self.versionSections:
            self.document.append(sect)
        self.versionSections = [] # In case we get called again
        self.document.write(fileName)
    
    def getFreeTextData(self, tests):
        data = seqdict()
        for testName, state, extraVersion in tests:
            freeText = state.freeText
            if freeText:
                if not data.has_key(freeText):
                    data[freeText] = []
                data[freeText].append((testName, state, extraVersion))
        return data.items()

    def getFullDescription(self, tests, version, linkFromDetailsToOverview):
        freeTextData = self.getFreeTextData(tests)
        if len(freeTextData) == 0:
            return
        fullText = HTMLgen.Container()
        for freeText, tests in freeTextData:
            tests.sort(key=lambda info: info[0])
            for testName, state, extraVersion in tests:
                fullText.append(HTMLgen.Name(version + testName + extraVersion))
            fullText.append(self.getHeaderLine(tests, version, linkFromDetailsToOverview))
            self.appendFreeText(fullText, freeText)
            if len(tests) > 1:
                for line in self.getTestLines(tests, version, linkFromDetailsToOverview):
                    fullText.append(line)                            
        return fullText
    
    def appendFreeText(self, fullText, freeText):
        freeText = freeText.replace("<", "&lt;").replace(">", "&gt;")
        linkMarker = "URL=http"
        if linkMarker in freeText:
            currFreeText = ""
            for line in freeText.splitlines():
                if linkMarker in line:
                    fullText.append(HTMLgen.RawText("<PRE>" + currFreeText.strip() + "</PRE>"))
                    currFreeText = ""
                    words = line.strip().split()
                    linkTarget = words[-1][4:] # strip off the URL=
                    newLine = " ".join(words[:-1])
                    fullText.append(HTMLgen.Href(linkTarget, newLine))
                    fullText.append(HTMLgen.BR())
                    fullText.append(HTMLgen.BR())
                else:
                    currFreeText += line + "\n"
        else:
            fullText.append(HTMLgen.RawText("<PRE>" + freeText + "</PRE>"))
    
    def getHeaderLine(self, tests, version, linkFromDetailsToOverview):
        testName, state, extraVersion = tests[0]
        if len(tests) == 1:
            linksToOverview = self.getLinksToOverview(version, testName, extraVersion, linkFromDetailsToOverview)
            headerText = "TEST " + repr(state) + " " + testName + " ("
            container = HTMLgen.Container(headerText, linksToOverview)
            return HTMLgen.Heading(4, container, ")")
        else:
            headerText = str(len(tests)) + " TESTS " + repr(state)
            return HTMLgen.Heading(4, headerText) 
    def getTestLines(self, tests, version, linkFromDetailsToOverview):
        lines = []
        for testName, state, extraVersion in tests:
            linksToOverview = self.getLinksToOverview(version, testName, extraVersion, linkFromDetailsToOverview)
            headerText = testName + " ("
            container = HTMLgen.Container(headerText, linksToOverview, ")<br>")
            lines.append(container)
        return lines
    def getLinksToOverview(self, version, testName, extraVersion, linkFromDetailsToOverview):
        links = HTMLgen.Container()
        for targetFile, linkName in linkFromDetailsToOverview:
            links.append(HTMLgen.Href(targetFile + "#" + version + testName + extraVersion, linkName))
        return links
        
class CategoryHandler:
    def __init__(self):
        self.testsInCategory = seqdict()

    def update(self, categoryHandler):
        for category, testInfo in categoryHandler.testsInCategory.items():
            testInfoList = self.testsInCategory.setdefault(category, [])
            testInfoList += testInfo

    def registerInCategory(self, testId, state, extraVersion):
        self.testsInCategory.setdefault(state.category, []).append((testId, state, extraVersion))

    def getDescription(self, cat, count):
        shortDescr, longDescr = getCategoryDescription(cat)
        return str(count) + " " + shortDescr

    def getTestCountDescription(self, count):
        return str(count) + " tests: "

    def generateTextSummary(self):
        numTests, summaryData = self.getSummaryData()
        categoryDescs = [ self.getDescription(cat, count) for cat, count in summaryData.items() ]
        return self.getTestCountDescription(numTests) + " ".join(categoryDescs)

    def generateHTMLSummary(self, detailPageRef, extraVersion=None):
        numTests, summaryData = self.getSummaryData(extraVersion)
        container = HTMLgen.Container()
        for cat, count in summaryData.items():
            summary = HTMLgen.Text(self.getDescription(cat, count))
            if cat == "success":
                container.append(summary)
            else:
                linkTarget = detailPageRef + getCategoryDescription(cat)[-1]
                container.append(HTMLgen.Href(linkTarget, summary))
            
        testCountSummary = HTMLgen.Text(self.getTestCountDescription(numTests))
        return HTMLgen.Container(testCountSummary, container)

    def countTests(self, testInfo, extraVersion):
        if extraVersion is not None:
            return sum((currExtra == extraVersion for (testId, state, currExtra) in testInfo))
        else:
            return len(testInfo)

    def getSummaryData(self, extraVersion=None):
        numTests = 0
        summaryData = seqdict()
        for cat, testInfo in sorted(self.testsInCategory.items(), self.compareCategories):
            testCount = self.countTests(testInfo, extraVersion)
            if testCount > 0:
                summaryData[cat] = testCount
                numTests += testCount
        return numTests, summaryData
    
    def getTestsWithDescriptions(self):
        return [ (getCategoryDescription(cat)[1], testInfo) for cat, testInfo in self.testsInCategory.items() ]

    def compareCategories(self, data1, data2):
        # Put success at the start, it's neater like that
        if data1[0] == "success":
            return -1
        elif data2[0] == "success":
            return 1
        else:
            return 0
                          

def getCategoryDescription(cat):
    return plugins.TestState.categoryDescriptions.get(cat, (cat, cat))

def getDetailPageName(pageVersion, tag):
    return "test_" + pageVersion + "_" + tag + ".html"


class BaseSelector(object):
    def __init__(self, linkName, suffix):
        self.selectedTags = []
        self.linkName = linkName
        self.suffix = suffix
    def add(self, tag):
        self.selectedTags.append(tag)
    def getLinkInfo(self, pageVersion):
        return "test_" + pageVersion + self.suffix + ".html", self.linkName


class Selector(BaseSelector):
    def __init__(self, linkName, suffix, getConfigValue, tags):
        super(Selector, self).__init__(linkName, suffix)
        cutoff = getConfigValue("historical_report_subpage_cutoff", linkName)
        weekdays = getConfigValue("historical_report_subpage_weekdays", linkName)
        self.selectedTags = tags[-cutoff:]
        if len(weekdays) > 0:
            self.selectedTags = filter(lambda tag: getWeekDay(tag) in weekdays, self.selectedTags)
    

class SelectorByMonth(BaseSelector):
    @classmethod
    def makeInstances(cls, tags):
        allSelectors = {}
        for tag in tags:
            month = tag[2:9]
            allSelectors.setdefault(month, SelectorByMonth(month)).add(tag)
        return sorted(allSelectors.values())
    def __init__(self, month):
        super(SelectorByMonth, self).__init__(month, "_all_" + month)
    def getMonthTime(self):
        return time.mktime(time.strptime(self.linkName, "%b%Y"))
    def __cmp__(self, other):
        return cmp(self.getMonthTime(), other.getMonthTime())
    def __eq__(self, other):
        return self.linkName == other.linkName
    def __hash__(self):
        return self.linkName.__hash__()
