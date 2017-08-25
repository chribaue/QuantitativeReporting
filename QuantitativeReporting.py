import getpass
import json
import logging
import slicer
import qt
import ctk
import vtk
import os

from slicer.ScriptedLoadableModule import *
import vtkSegmentationCorePython as vtkSegmentationCore

from SlicerDevelopmentToolboxUtils.mixins import ModuleWidgetMixin, ModuleLogicMixin, ParameterNodeObservationMixin
from SlicerDevelopmentToolboxUtils.decorators import onExceptionReturnNone, postCall
from SlicerDevelopmentToolboxUtils.helpers import WatchBoxAttribute
from SlicerDevelopmentToolboxUtils.widgets import DICOMBasedInformationWatchBox, ImportLabelMapIntoSegmentationWidget
from SlicerDevelopmentToolboxUtils.widgets import CopySegmentBetweenSegmentationsWidget
from SlicerDevelopmentToolboxUtils.constants import DICOMTAGS
from SlicerDevelopmentToolboxUtils.buttons import RedSliceLayoutButton, FourUpLayoutButton, FourUpTableViewLayoutButton
from SlicerDevelopmentToolboxUtils.buttons import CrosshairButton

from SegmentEditor import SegmentEditorWidget
from SegmentStatistics import SegmentStatisticsLogic, SegmentStatisticsParameterEditorDialog

from DICOMLib.DICOMWidgets import DICOMDetailsWidget
from DICOMSegmentationPlugin import DICOMSegmentationExporter


class QuantitativeReporting(ScriptedLoadableModule):
  """Uses ScriptedLoadableModule base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def __init__(self, parent):
    ScriptedLoadableModule.__init__(self, parent)
    self.parent.title = "Quantitative Reporting"
    self.parent.categories = ["Informatics", "Quantification", "Segmentation"]
    self.parent.dependencies = ["SlicerDevelopmentToolbox"]
    self.parent.contributors = ["Christian Herz (SPL, BWH), Andrey Fedorov (SPL, BWH), "
                                "Csaba Pinter (Queen's), Andras Lasso (Queen's), Steve Pieper (Isomics)"]
    self.parent.helpText = """
    Segmentation-based measurements with DICOM-based import and export of the results.
    <a href="https://qiicr.gitbooks.io/quantitativereporting-guide">Documentation.</a>
    """
    self.parent.acknowledgementText = """
    This work was supported in part by the National Cancer Institute funding to the
    Quantitative Image Informatics for Cancer Research (QIICR) (U24 CA180918).
    """


class QuantitativeReportingWidget(ModuleWidgetMixin, ScriptedLoadableModuleWidget):
  """Uses ScriptedLoadableModuleWidget base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def __init__(self, parent=None):
    ScriptedLoadableModuleWidget.__init__(self, parent)
    self.segmentationsLogic = slicer.modules.segmentations.logic()
    self.slicerTempDir = slicer.util.tempDirectory()
    slicer.mrmlScene.AddObserver(slicer.mrmlScene.EndCloseEvent, self.onSceneClosed)

  def initializeMembers(self):
    self.tableNode = None
    self.segmentationObservers = []
    self.dicomSegmentationExporter = None

  def enter(self):
    self.measurementReportSelector.setCurrentNode(None)
    self.segmentEditorWidget.editor.setSegmentationNode(None)
    self.segmentEditorWidget.editor.setMasterVolumeNode(None)
    self.segmentEditorWidget.editor.masterVolumeNodeChanged.connect(self.onImageVolumeSelected)
    self.segmentEditorWidget.editor.segmentationNodeChanged.connect(self.onSegmentationSelected)
    # self.setupDICOMBrowser()
    qt.QTimer.singleShot(0, lambda: self.updateSizes(self.tabWidget.currentIndex))

  def exit(self):
    self.segmentEditorWidget.editor.masterVolumeNodeChanged.disconnect(self.onImageVolumeSelected)
    self.segmentEditorWidget.editor.segmentationNodeChanged.disconnect(self.onSegmentationSelected)
    # self.removeDICOMBrowser()
    qt.QTimer.singleShot(0, lambda: self.tabWidget.setCurrentIndex(0))

  def onReload(self):
    self.cleanupUIElements()
    self.removeAllUIElements()
    super(QuantitativeReportingWidget, self).onReload()

  def onSceneClosed(self, caller, event):
    if hasattr(self, "watchBox"):
      self.watchBox.reset()
    if hasattr(self, "testArea"):
      self.retrieveTestDataButton.enabled = True

  def cleanupUIElements(self):
    self.removeSegmentationObserver()
    self.removeConnections()
    self.initializeMembers()

  def removeAllUIElements(self):
    for child in [c for c in self.parent.children() if not isinstance(c, qt.QVBoxLayout)]:
      try:
        child.delete()
      except AttributeError:
        pass

  def refreshUIElementsAvailability(self):

    def refresh():
      self.segmentEditorWidget.editor.masterVolumeNodeSelectorVisible = \
        self.measurementReportSelector.currentNode() and \
        not ModuleLogicMixin.getReferencedVolumeFromSegmentationNode(self.segmentEditorWidget.segmentationNode)
      masterVolume = self.segmentEditorWidget.masterVolumeNode
      self.importSegmentationCollapsibleButton.enabled = masterVolume is not None
      if not self.importSegmentationCollapsibleButton.collapsed:
        self.importSegmentationCollapsibleButton.collapsed = masterVolume is None

      self.importLabelMapCollapsibleButton.enabled = masterVolume is not None
      if not self.importLabelMapCollapsibleButton.collapsed:
        self.importLabelMapCollapsibleButton.collapsed = masterVolume is None
      if not self.tableNode:
        self.enableReportButtons(False)
        self.updateMeasurementsTable(triggered=True)

    qt.QTimer.singleShot(0, refresh)

  @postCall(refreshUIElementsAvailability)
  def setup(self):
    ScriptedLoadableModuleWidget.setup(self)

    self.initializeMembers()
    self.setupTabBarNavigation()
    self.setupWatchBox()
    self.setupViewSettingsArea()
    self.setupTestArea()
    self.setupSegmentationsArea()
    self.setupSelectionArea()
    self.setupImportArea()
    self.mainModuleWidgetLayout.addWidget(self.segmentationGroupBox)
    self.setupMeasurementsArea()
    self.setupActionButtons()

    self.setupConnections()
    self.layout.addStretch(1)
    self.fourUpSliceLayoutButton.checked = True

  def setupTabBarNavigation(self):
    self.tabWidget = qt.QTabWidget()
    self.layout.addWidget(self.tabWidget)

    self.mainModuleWidget = qt.QWidget()

    self.mainModuleWidgetLayout = qt.QGridLayout()

    self.mainModuleWidget.setLayout(self.mainModuleWidgetLayout)

    self.tabWidget.setIconSize(qt.QSize(85, 30))
    self.tabWidget.addTab(self.mainModuleWidget, 'QR')

  def setupDICOMBrowser(self):
    self.dicomBrowser = CustomDICOMDetailsWidget()
    self.dicomBrowser.addEventObserver(CustomDICOMDetailsWidget.FinishedLoadingEvent, self.onLoadingFinishedEvent)
    self.tabWidget.addTab(self.dicomBrowser, 'DICOM')

  def removeDICOMBrowser(self):
    if not self.dicomBrowser:
      return
    self.dicomBrowser.removeEventObserver(CustomDICOMDetailsWidget.FinishedLoadingEvent, self.onLoadingFinishedEvent)
    self.tabWidget.removeTab(self.tabWidget.indexOf(self.dicomBrowser))
    self.dicomBrowser = None

  def enableReportButtons(self, enabled):
    self.saveReportButton.enabled = enabled
    self.completeReportButton.enabled = enabled

  def setupWatchBox(self):
    self.watchBoxInformation = [
      WatchBoxAttribute('StudyID', 'Study ID: ', DICOMTAGS.STUDY_ID),
      WatchBoxAttribute('PatientName', 'Patient Name: ', DICOMTAGS.PATIENT_NAME),
      WatchBoxAttribute('DOB', 'Date of Birth: ', DICOMTAGS.PATIENT_BIRTH_DATE),
      WatchBoxAttribute('Reader', 'Reader Name: ', callback=getpass.getuser)]
    self.watchBox = DICOMBasedInformationWatchBox(self.watchBoxInformation)
    self.mainModuleWidgetLayout.addWidget(self.watchBox)

  def setupTestArea(self):

    def loadTestData():
      mrHeadSeriesUID = "2.16.840.1.113662.4.4168496325.1025306170.548651188813145058"
      if not len(slicer.dicomDatabase.filesForSeries(mrHeadSeriesUID)):
        sampleData = TestDataLogic.downloadSampleData()
        unzipped = TestDataLogic.unzipSampleData(sampleData)
        TestDataLogic.importIntoDICOMDatabase(unzipped)
      self.loadSeries(mrHeadSeriesUID)
      masterNode = slicer.util.getNode('2: SAG*')
      tableNode = slicer.vtkMRMLTableNode()
      tableNode.SetAttribute("QuantitativeReporting", "Yes")
      slicer.mrmlScene.AddNode(tableNode)
      self.measurementReportSelector.setCurrentNode(tableNode)
      self.segmentEditorWidget.editor.setMasterVolumeNode(masterNode)
      self.retrieveTestDataButton.enabled = False

    self.testArea = qt.QGroupBox("Test Area")
    self.testAreaLayout = qt.QFormLayout(self.testArea)
    self.retrieveTestDataButton = self.createButton("Retrieve and load test data")
    self.testAreaLayout.addWidget(self.retrieveTestDataButton)
    self.retrieveTestDataButton.clicked.connect(loadTestData)
    self.mainModuleWidgetLayout.addWidget(self.testArea)

  def loadSeriesByFileName(self, filename):
    seriesUID = slicer.dicomDatabase.seriesForFile(filename)
    self.loadSeries(seriesUID)

  def loadSeries(self, seriesUID):
    dicomWidget = slicer.modules.dicom.widgetRepresentation().self()
    dicomWidget.detailsPopup.offerLoadables(seriesUID, 'Series')
    dicomWidget.detailsPopup.examineForLoading()
    dicomWidget.detailsPopup.loadCheckedLoadables()

  def setupSelectionArea(self):
    self.segmentEditorWidget.editor.masterVolumeNodeSelectorAddAttribute("vtkMRMLScalarVolumeNode",
                                                                         "DICOM.instanceUIDs", None)
    self.measurementReportSelector = self.createComboBox(nodeTypes=["vtkMRMLTableNode", ""], showChildNodeTypes=False,
                                                         addEnabled=True, removeEnabled=True, noneEnabled=True,
                                                         selectNodeUponCreation=True,
                                                         toolTip="Select measurement report")
    self.measurementReportSelector.addAttribute("vtkMRMLTableNode", "QuantitativeReporting", "Yes")

    self.selectionAreaWidget = qt.QWidget()
    self.selectionAreaWidgetLayout = qt.QGridLayout()
    self.selectionAreaWidget.setLayout(self.selectionAreaWidgetLayout)

    self.selectionAreaWidgetLayout.addWidget(qt.QLabel("Measurement report"), 0, 0)
    self.selectionAreaWidgetLayout.addWidget(self.measurementReportSelector, 0, 1)
    self.mainModuleWidgetLayout.addWidget(self.selectionAreaWidget)

  def setupImportArea(self):
    self.setupImportSegmentation()
    self.setupImportLabelmap()

  def setupImportSegmentation(self):
    self.importSegmentationCollapsibleButton = ctk.ctkCollapsibleButton()
    self.importSegmentationCollapsibleButton.collapsed = True
    self.importSegmentationCollapsibleButton.enabled = False
    self.importSegmentationCollapsibleButton.text = "Import from segmentation"
    self.importSegmentsCollapsibleLayout = qt.QGridLayout(self.importSegmentationCollapsibleButton)

    self.segmentImportWidget = CopySegmentBetweenSegmentationsWidget()
    self.segmentImportWidget.addEventObserver(self.segmentImportWidget.FailedEvent, self.onImportFailed)
    self.segmentImportWidget.currentSegmentationNodeSelectorEnabled = False
    self.importSegmentsCollapsibleLayout.addWidget(self.segmentImportWidget)
    self.mainModuleWidgetLayout.addWidget(self.importSegmentationCollapsibleButton)

  def setupImportLabelmap(self):
    self.importLabelMapCollapsibleButton = ctk.ctkCollapsibleButton()
    self.importLabelMapCollapsibleButton.collapsed = True
    self.importLabelMapCollapsibleButton.enabled = False
    self.importLabelMapCollapsibleButton.text = "Import from labelmap"
    self.importLabelMapCollapsibleLayout = qt.QGridLayout(self.importLabelMapCollapsibleButton)

    self.labelMapImportWidget = ImportLabelMapIntoSegmentationWidget()
    self.labelMapImportWidget.addEventObserver(self.labelMapImportWidget.FailedEvent, self.onImportFailed)
    self.labelMapImportWidget.addEventObserver(self.labelMapImportWidget.SuccessEvent, self.onLabelMapImportSuccessful)
    self.labelMapImportWidget.segmentationNodeSelectorVisible = False
    self.importLabelMapCollapsibleLayout.addWidget(self.labelMapImportWidget)
    self.mainModuleWidgetLayout.addWidget(self.importLabelMapCollapsibleButton)

  def onImportFailed(self, caller, event):
    slicer.util.errorDisplay("Import failed. Check console for details.")

  def onLabelMapImportSuccessful(self, caller, event):
    self.hideAllLabels()

  def setupViewSettingsArea(self):
    self.redSliceLayoutButton = RedSliceLayoutButton()
    self.fourUpSliceLayoutButton = FourUpLayoutButton()
    self.fourUpSliceTableViewLayoutButton = FourUpTableViewLayoutButton()
    self.crosshairButton = CrosshairButton()
    self.crosshairButton.setSliceIntersectionEnabled(True)

    hbox = self.createHLayout([self.redSliceLayoutButton, self.fourUpSliceLayoutButton,
                               self.fourUpSliceTableViewLayoutButton, self.crosshairButton])
    hbox.layout().addStretch(1)
    self.mainModuleWidgetLayout.addWidget(hbox)

  def setupSegmentationsArea(self):
    self.segmentationGroupBox = qt.QGroupBox("Segmentations")
    self.segmentationGroupBoxLayout = qt.QFormLayout()
    self.segmentationGroupBox.setLayout(self.segmentationGroupBoxLayout)
    self.segmentEditorWidget = QuantitativeReportingSegmentEditorWidget(parent=self.segmentationGroupBox)
    self.segmentEditorWidget.setup()

  def setupMeasurementsArea(self):
    self.measurementsGroupBox = qt.QGroupBox("Measurements")
    self.measurementsGroupBoxLayout = qt.QVBoxLayout()
    self.measurementsGroupBox.setLayout(self.measurementsGroupBoxLayout)
    self.tableView = slicer.qMRMLTableView()
    self.tableView.setMinimumHeight(150)
    self.tableView.setMaximumHeight(150)
    self.tableView.setSelectionBehavior(qt.QTableView.SelectRows)
    self.tableView.horizontalHeader().setResizeMode(qt.QHeaderView.Stretch)
    self.fourUpTableView = None
    self.segmentStatisticsConfigButton = self.createButton("Segment Statistics Parameters")
    self.measurementsGroupBoxLayout.addWidget(self.segmentStatisticsConfigButton)
    self.measurementsGroupBoxLayout.addWidget(self.tableView)
    self.mainModuleWidgetLayout.addWidget(self.measurementsGroupBox)

  def setupActionButtons(self):
    self.calculateMeasurementsButton = self.createButton("Calculate Measurements", enabled=False)
    self.calculateAutomaticallyCheckbox = qt.QCheckBox("Auto Update")
    self.calculateAutomaticallyCheckbox.checked = True
    self.saveReportButton = self.createButton("Save Report")
    self.completeReportButton = self.createButton("Complete Report")
    self.enableReportButtons(False)
    self.mainModuleWidgetLayout.addWidget(self.createHLayout([self.calculateMeasurementsButton,
                                                              self.calculateAutomaticallyCheckbox]))
    self.mainModuleWidgetLayout.addWidget(self.createHLayout([self.saveReportButton, self.completeReportButton]))

  def setupConnections(self, funcName="connect"):

    def setupSelectorConnections():
      getattr(self.measurementReportSelector, funcName)('currentNodeChanged(vtkMRMLNode*)',
                                                        self.onMeasurementReportSelected)

    def setupButtonConnections():
      getattr(self.saveReportButton.clicked, funcName)(self.onSaveReportButtonClicked)
      getattr(self.completeReportButton.clicked, funcName)(self.onCompleteReportButtonClicked)
      getattr(self.calculateMeasurementsButton.clicked, funcName)(lambda: self.updateMeasurementsTable(triggered=True))
      getattr(self.segmentStatisticsConfigButton.clicked, funcName)(self.onEditParameters)

    def setupOtherConnections():
      getattr(self.layoutManager.layoutChanged, funcName)(self.onLayoutChanged)
      getattr(self.layoutManager.layoutChanged, funcName)(self.setupFourUpTableViewConnection)
      getattr(self.calculateAutomaticallyCheckbox.toggled, funcName)(self.onCalcAutomaticallyToggled)
      getattr(self.segmentEditorWidget.editor.currentSegmentIDChanged, funcName)(self.onCurrentSegmentIDChanged)
      getattr(self.tableView.selectionModel().selectionChanged, funcName)(self.onSegmentSelectionChanged)
      getattr(self.tabWidget.currentChanged, funcName)(self.onTabWidgetClicked)

    setupSelectorConnections()
    setupButtonConnections()
    setupOtherConnections()

  def onEditParameters(self, calculatorName=None):
    """Open dialog box to edit calculator's parameters"""
    pNode = self.segmentEditorWidget.logic.segmentStatisticsLogic.getParameterNode()
    if pNode:
      SegmentStatisticsParameterEditorDialog.editParameters(pNode,calculatorName)
      self.updateMeasurementsTable(triggered=True)

  def onTabWidgetClicked(self, currentIndex):
    if currentIndex == 0:
      slicer.app.layoutManager().parent().parent().show()
      self.dicomBrowser.close()
    elif currentIndex == 1:
      slicer.app.layoutManager().parent().parent().hide()
      self.dicomBrowser.open()

    qt.QTimer.singleShot(0, lambda: self.updateSizes(currentIndex))

  def updateSizes(self, index):
    mainWindow = slicer.util.mainWindow()
    dockWidget = slicer.util.findChildren(mainWindow, name='dockWidgetContents')[0]
    tempPolicy = dockWidget.sizePolicy
    if index==0:
      dockWidget.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Preferred)
    qt.QTimer.singleShot(0, lambda: dockWidget.setSizePolicy(tempPolicy))

  def onCurrentSegmentIDChanged(self, segmentID):
    if segmentID == '':
      return
    selectedRow = self.segmentEditorWidget.getSegmentIndexByID(segmentID)
    self.onSegmentSelected(selectedRow)

  def onSegmentSelectionChanged(self, itemSelection):
    selectedRow = itemSelection.indexes()[0].row() if len(itemSelection.indexes()) else None
    if selectedRow is not None:
      self.onSegmentSelected(selectedRow)

  def onSegmentSelected(self, index):
    segmentID = self.segmentEditorWidget.getSegmentIDByIndex(index)
    self.segmentEditorWidget.editor.setCurrentSegmentID(segmentID)
    self.selectRowIfNotSelected(self.tableView, index)
    self.selectRowIfNotSelected(self.fourUpTableView, index)
    self.segmentEditorWidget.onSegmentSelected(index)

  def selectRowIfNotSelected(self, tableView, selectedRow):
    if tableView:
      if len(tableView.selectedIndexes()):
        if tableView.selectedIndexes()[0].row() != selectedRow:
          tableView.selectRow(selectedRow)
      elif tableView.model().rowCount() > selectedRow:
        tableView.selectRow(selectedRow)

  def removeConnections(self):
    self.setupConnections(funcName="disconnect")
    if self.fourUpTableView:
      self.fourUpTableView.selectionModel().selectionChanged.disconnect(self.onSegmentSelectionChanged)

  def onCalcAutomaticallyToggled(self, checked):
    if checked and self.segmentEditorWidget.segmentation is not None:
      self.updateMeasurementsTable(triggered=True)
    self.calculateMeasurementsButton.enabled = not checked and self.tableNode

  def removeSegmentationObserver(self):
    if self.segmentEditorWidget.segmentation and len(self.segmentationObservers):
      while len(self.segmentationObservers):
        observer = self.segmentationObservers.pop()
        self.segmentEditorWidget.segmentation.RemoveObserver(observer)

  def setupFourUpTableViewConnection(self):
    if not self.fourUpTableView and self.layoutManager.layout == self.fourUpSliceTableViewLayoutButton.LAYOUT:
      if slicer.app.layoutManager().tableWidget(0):
        self.fourUpTableView = slicer.app.layoutManager().tableWidget(0).tableView()
        self.fourUpTableView.selectionModel().selectionChanged.connect(self.onSegmentSelectionChanged)
        self.fourUpTableView.setSelectionBehavior(qt.QTableView.SelectRows)

  def onLoadingFinishedEvent(self, caller, event):
    self.tabWidget.setCurrentIndex(0)

  def onLayoutChanged(self):
    self.onDisplayMeasurementsTable()
    self.onCurrentSegmentIDChanged(self.segmentEditorWidget.editor.currentSegmentID())

  @postCall(refreshUIElementsAvailability)
  def onSegmentationSelected(self, node):
    if not node:
      return
    masterVolume = ModuleLogicMixin.getReferencedVolumeFromSegmentationNode(node)
    if masterVolume:
      self.initializeWatchBox(masterVolume)

  @postCall(refreshUIElementsAvailability)
  def onImageVolumeSelected(self, node):
    self.seriesNumber = None
    self.initializeWatchBox(node)

  @postCall(refreshUIElementsAvailability)
  def onMeasurementReportSelected(self, node):
    self.removeSegmentationObserver()
    self.segmentEditorWidget.editor.setMasterVolumeNode(None)
    self.calculateAutomaticallyCheckbox.checked = True
    self.tableNode = node
    self.hideAllSegmentations()
    if node is None:
      self.segmentEditorWidget.editor.setSegmentationNode(None)
      self.updateImportArea(None)
      self.watchBox.reset()
      return

    segmentationNode = self._getOrCreateSegmentationNodeAndConfigure()
    self.updateImportArea(segmentationNode)
    self._setupSegmentationObservers()
    self._configureReadWriteAccess()

  def _configureReadWriteAccess(self):
    if not self.tableNode:
      return
    if self.tableNode.GetAttribute("readonly"):
      logging.debug("Selected measurements report is readonly")
      self.setMeasurementsTable(self.tableNode)
      self.segmentEditorWidget.enabled = False
      self.enableReportButtons(False)
      self.calculateAutomaticallyCheckbox.enabled = False
    else:
      self.segmentEditorWidget.enabled = True
      self.calculateAutomaticallyCheckbox.enabled = True
      self.onSegmentationNodeChanged()

  def _getOrCreateSegmentationNodeAndConfigure(self):
    segmentationNodeID = self.tableNode.GetAttribute('ReferencedSegmentationNodeID')
    logging.debug("ReferencedSegmentationNodeID {}".format(segmentationNodeID))
    if segmentationNodeID:
      segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationNodeID)
    else:
      segmentationNode = self._createAndReferenceNewSegmentationNode()
    self._configureSegmentationNode(segmentationNode)
    return segmentationNode

  def _configureSegmentationNode(self, node):
    self.hideAllSegmentations()
    self.segmentEditorWidget.editor.setSegmentationNode(node)
    node.SetDisplayVisibility(True)

  def _createAndReferenceNewSegmentationNode(self):
    segmentationNode = self.createNewSegmentationNode()
    self.tableNode.SetAttribute('ReferencedSegmentationNodeID', segmentationNode.GetID())
    return segmentationNode

  def updateImportArea(self, node):
    self.segmentImportWidget.otherSegmentationNodeSelector.setCurrentNode(None)
    self.segmentImportWidget.setCurrentSegmentationNode(node)
    self.labelMapImportWidget.setSegmentationNode(node)

  def _setupSegmentationObservers(self):
    segNode = self.segmentEditorWidget.segmentation
    if not segNode:
      return
    segmentationEvents = [vtkSegmentationCore.vtkSegmentation.SegmentAdded,
                          vtkSegmentationCore.vtkSegmentation.SegmentRemoved,
                          vtkSegmentationCore.vtkSegmentation.SegmentModified,
                          vtkSegmentationCore.vtkSegmentation.RepresentationModified]
    for event in segmentationEvents:
      self.segmentationObservers.append(segNode.AddObserver(event, self.onSegmentationNodeChanged))

  def initializeWatchBox(self, node):
    if not node:
      self.watchBox.sourceFile = None
      return
    try:
      dicomFileName = slicer.dicomDatabase.fileForInstance(node.GetAttribute("DICOM.instanceUIDs").split(" ")[0])
      self.watchBox.sourceFile = dicomFileName
    except AttributeError:
      self.watchBox.sourceFile = None
      if slicer.util.confirmYesNoDisplay("The referenced master volume from the current segmentation is not of type "
                                         "DICOM. QuantitativeReporting will create a new segmentation node for the "
                                         "current measurement report. You will need to select a proper DICOM master "
                                         "volume in order to create a segmentation. Do you want to proceed?",
                                         detailedText="In some cases a non DICOM master volume was selected from the "
                                                      "SegmentEditor module itself. QuantitativeReporting currently "
                                                      "does not support non DICOM master volumes."):
        self._configureSegmentationNode(self._createAndReferenceNewSegmentationNode())
        self.segmentEditorWidget.editor.setMasterVolumeNode(None)
      else:
        self.measurementReportSelector.setCurrentNode(None)

  def createNewSegmentationNode(self):
    return slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")

  @postCall(refreshUIElementsAvailability)
  def onSegmentationNodeChanged(self, observer=None, caller=None):
    self.enableReportButtons(True)
    self.updateMeasurementsTable()

  def updateMeasurementsTable(self, triggered=False, visibleOnly=False):
    if not self.calculateAutomaticallyCheckbox.checked and not triggered:
      self.tableView.setStyleSheet("QTableView{border:2px solid red;};")
      return
    table = self.segmentEditorWidget.calculateSegmentStatistics(self.tableNode, visibleOnly)
    self.setMeasurementsTable(table)

  def setMeasurementsTable(self, table):
    if table:
      self.tableNode = table
      self.tableNode.SetLocked(True)
      self.tableView.setMRMLTableNode(self.tableNode)
      self.tableView.setStyleSheet("QTableView{border:none};")
    else:
      if self.tableNode:
        self.tableNode.RemoveAllColumns()
      self.tableView.setMRMLTableNode(self.tableNode if self.tableNode else None)
    self.onDisplayMeasurementsTable()

  def onDisplayMeasurementsTable(self):
    self.tableView.visible = not self.layoutManager.layout == self.fourUpSliceTableViewLayoutButton.LAYOUT
    if self.layoutManager.layout == self.fourUpSliceTableViewLayoutButton.LAYOUT and self.tableNode:
      slicer.app.applicationLogic().GetSelectionNode().SetReferenceActiveTableID(self.tableNode.GetID())
      slicer.app.applicationLogic().PropagateTableSelection()

  def onSaveReportButtonClicked(self):
    success = self.saveReport()
    self.saveReportButton.enabled = not success
    if success:
      slicer.util.infoDisplay("Report successfully saved into SlicerDICOMDatabase")

  def onCompleteReportButtonClicked(self):
    success = self.saveReport(completed=True)
    self.saveReportButton.enabled = not success
    self.completeReportButton.enabled = not success
    if success:
      slicer.util.infoDisplay("Report successfully completed and saved into SlicerDICOMDatabase")
      self.tableNode.SetAttribute("readonly", "Yes")

  def saveReport(self, completed=False):
    try:
      self.dicomSegmentationExporter = DICOMSegmentationExporter(self.segmentEditorWidget.segmentationNode)
      dcmSegmentationPath = "quantitative_reporting_export.SEG" + self.dicomSegmentationExporter.currentDateTime + ".dcm"
      self.createSEG(dcmSegmentationPath)
      self.createDICOMSR(dcmSegmentationPath, completed)
      self.addProducedDataToDICOMDatabase()
    except (RuntimeError, ValueError, AttributeError) as exc:
      slicer.util.warningDisplay(exc.message)
      return False
    finally:
      self.cleanupTemporaryData()
    return True

  def createSEG(self, dcmSegmentationPath):
    segmentIDs = None
    if self.segmentEditorWidget.hiddenSegmentsAvailable():
      if not slicer.util.confirmYesNoDisplay("Hidden segments have been found. Do you want to export them as well?"):
        self.updateMeasurementsTable(visibleOnly=True)
        visibileSegments = self.segmentEditorWidget.logic.getVisibleSegments(self.segmentEditorWidget.segmentationNode)
        segmentIDs = [segment.GetName() for segment in visibileSegments]
    try:
      try:
        self.dicomSegmentationExporter.export(dcmSegmentationPath, segmentIDs=segmentIDs)
      except DICOMSegmentationExporter.EmptySegmentsFoundError:
        raise ValueError("Empty segments found. Please make sure that there are no empty segments.")
      slicer.dicomDatabase.insert(dcmSegmentationPath)
      logging.info("Added segmentation to DICOM database (%s)", dcmSegmentationPath)
    except (DICOMSegmentationExporter.NoNonEmptySegmentsFoundError, ValueError) as exc:
      raise ValueError(exc.message)

    logging.debug("Saved DICOM Segmentation to {}".format(dcmSegmentationPath))

  def createDICOMSR(self, referencedSegmentation, completed):
    data = self.dicomSegmentationExporter.getSeriesAttributes()
    data["SeriesDescription"] = "Measurement Report"

    compositeContextDataDir, data["compositeContext"] = \
      os.path.dirname(referencedSegmentation), [os.path.basename(referencedSegmentation)]
    imageLibraryDataDir, data["imageLibrary"] = \
      self.dicomSegmentationExporter.getDICOMFileList(self.segmentEditorWidget.masterVolumeNode)
    data.update(self._getAdditionalSRInformation(completed))

    data["Measurements"] = \
      self.segmentEditorWidget.logic.segmentStatisticsLogic.generateJSON4DcmSR(referencedSegmentation,
                                                                               self.segmentEditorWidget.masterVolumeNode)
    logging.debug("DICOM SR Metadata output:")
    logging.debug(json.dumps(data, indent=2, separators=(',', ': ')))

    metaFilePath = self.saveJSON(data, os.path.join(self.dicomSegmentationExporter.tempDir, "sr_meta.json"))
    outputSRPath = os.path.join(self.dicomSegmentationExporter.tempDir, "sr.dcm")

    params = {"metaDataFileName": metaFilePath,
              "compositeContextDataDir": compositeContextDataDir,
              "imageLibraryDataDir": imageLibraryDataDir,
              "outputFileName": outputSRPath}

    logging.debug(params)
    cliNode = None
    cliNode = slicer.cli.run(slicer.modules.tid1500writer, cliNode, params, wait_for_completion=True)
    waitCount = 0
    while cliNode.IsBusy() and waitCount < 20:
      slicer.util.delayDisplay("Running SR Encoding... %d" % waitCount, 1000)
      waitCount += 1

    if cliNode.GetStatusString() != 'Completed':
      raise Exception("tid1500writer CLI did not complete cleanly")

  def addProducedDataToDICOMDatabase(self):
    indexer = ctk.ctkDICOMIndexer()
    indexer.addDirectory(slicer.dicomDatabase, self.dicomSegmentationExporter.tempDir, "copy")  # TODO: doesn't really expect a destination dir

  def cleanupTemporaryData(self):
    if self.dicomSegmentationExporter:
      self.dicomSegmentationExporter.cleanup()
    self.dicomSegmentationExporter = None

  def _getAdditionalSRInformation(self, completed=False):
    data = dict()
    data["observerContext"] = {"ObserverType": "PERSON",
                               "PersonObserverName": self.watchBox.getAttribute("Reader").value}
    data["VerificationFlag"] = "VERIFIED" if completed else "UNVERIFIED"
    data["CompletionFlag"] = "COMPLETE" if completed else "PARTIAL"
    data["activitySession"] = "1"
    data["timePoint"] = "1"
    return data

  def saveJSON(self, data, destination):
    with open(os.path.join(destination), 'w') as outfile:
      json.dump(data, outfile, indent=2)
    return destination


class QuantitativeReportingTest(ScriptedLoadableModuleTest):
  """
  This is the test case for your scripted module.
  Uses ScriptedLoadableModuleTest base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def setUp(self):
    """ Do whatever is needed to reset the state - typically a scene clear will be enough.
    """
    slicer.mrmlScene.Clear(0)

  def runTest(self):
    """Run as few or as many tests as needed here.
    """
    self.setUp()
    self.test_Reporting1()

  def test_Reporting1(self):
    """ Ideally you should have several levels of tests.  At the lowest level
    tests should exercise the functionality of the logic with different inputs
    (both valid and invalid).  At higher levels your tests should emulate the
    way the user would interact with your code and confirm that it still works
    the way you intended.
    One of the most important features of the tests is that it should alert other
    developers when their changes will have an impact on the behavior of your
    module.  For example, if a developer removes a feature that you depend on,
    your test should break so they know that the feature is needed.
    """

    self.delayDisplay("Starting the test")
    #
    # first, get some data
    #
    import urllib
    downloads = (
        ('http://slicer.kitware.com/midas3/download?items=5767', 'FA.nrrd', slicer.util.loadVolume),
        )

    for url,name,loader in downloads:
      filePath = slicer.app.temporaryPath + '/' + name
      if not os.path.exists(filePath) or os.stat(filePath).st_size == 0:
        logging.info('Requesting download %s from %s...\n' % (name, url))
        urllib.urlretrieve(url, filePath)
      if loader:
        logging.info('Loading %s...' % (name,))
        loader(filePath)
    self.delayDisplay('Finished with download and loading')
    self.delayDisplay('Test passed!')


class QuantitativeReportingSegmentEditorWidget(SegmentEditorWidget, ModuleWidgetMixin):

  @property
  def segmentationNode(self):
    return self.editor.segmentationNode()

  @property
  def masterVolumeNode(self):
    return self.editor.masterVolumeNode()

  @property
  @onExceptionReturnNone
  def segmentation(self):
    return self.segmentationNode.GetSegmentation()

  @property
  def segments(self):
    if not self.segmentationNode:
      return []
    return self.logic.getAllSegments(self.segmentationNode)

  @property
  def enabled(self):
    return self.editor.enabled

  @enabled.setter
  def enabled(self, enabled):
    self.editor.setReadOnly(not enabled)

  def __init__(self, parent):
    SegmentEditorWidget.__init__(self, parent)
    self.logic = QuantitativeReportingSegmentEditorLogic()

  def setup(self):
    super(QuantitativeReportingSegmentEditorWidget, self).setup()
    if self.developerMode:
      self.reloadCollapsibleButton.hide()
    self.editor.segmentationNodeSelectorVisible = False
    self.editor.setEffectButtonStyle(qt.Qt.ToolButtonIconOnly)
    self.clearSegmentationEditorSelectors()

  def onSegmentSelected(self, selectedRow):
    try:
      segment = self.segments[selectedRow]
      self.jumpToSegmentCenter(segment)
    except IndexError:
      pass

  def jumpToSegmentCenter(self, segment):
    centroid = self.logic.getSegmentCentroid(self.segmentationNode, segment)
    if centroid:
      markupsLogic = slicer.modules.markups.logic()
      markupsLogic.JumpSlicesToLocation(centroid[0], centroid[1], centroid[2], False)

  def clearSegmentationEditorSelectors(self):
    self.editor.setSegmentationNode(None)
    self.editor.setMasterVolumeNode(None)

  def enter(self):
    # overridden because SegmentEditorWidget automatically creates a new Segmentation upon instantiation
    self.turnOffLightboxes()
    self.installShortcutKeys()

    # Set parameter set node if absent
    self.selectParameterNode()
    self.editor.updateWidgetFromMRML()

  def calculateSegmentStatistics(self, tableNode, visibleOnly):
    if not self.segmentationNode or not self.masterVolumeNode:
      return None
    return self.logic.calculateSegmentStatistics(self.segmentationNode, self.masterVolumeNode, visibleOnly, tableNode)

  def hiddenSegmentsAvailable(self):
    return len(self.logic.getAllSegments(self.segmentationNode)) \
           != len(self.logic.getVisibleSegments(self.segmentationNode))

  def getSegmentIndexByID(self, segmentID):
    return self.logic.getSegmentIndexByID(self.segmentationNode, segmentID)

  def getSegmentIDByIndex(self, index):
    return self.logic.getSegmentIDs(self.segmentationNode, False)[index]


class QuantitativeReportingSegmentEditorLogic(ScriptedLoadableModuleLogic):

  def __init__(self, parent=None):
    ScriptedLoadableModuleLogic.__init__(self, parent)
    self.parent = parent
    self.volumesLogic = slicer.modules.volumes.logic()
    self.segmentationsLogic = slicer.modules.segmentations.logic()
    self.segmentStatisticsLogic = CustomSegmentStatisticsLogic()

  def getSegmentIDs(self, segmentationNode, visibleOnly):
    if not segmentationNode:
      return []
    segmentIDs = vtk.vtkStringArray()
    segmentation = segmentationNode.GetSegmentation()
    command = segmentationNode.GetDisplayNode().GetVisibleSegmentIDs if visibleOnly else segmentation.GetSegmentIDs
    command(segmentIDs)
    return [segmentIDs.GetValue(idx) for idx in range(segmentIDs.GetNumberOfValues())]

  def getAllSegments(self, segmentationNode):
    segmentation = segmentationNode.GetSegmentation()
    return [segmentation.GetSegment(segmentID) for segmentID in self.getSegmentIDs(segmentationNode, False)]

  def getVisibleSegments(self, segmentationNode):
    segmentation = segmentationNode.GetSegmentation()
    return [segmentation.GetSegment(segmentID) for segmentID in self.getSegmentIDs(segmentationNode, True)]

  def getSegmentIndexByID(self, segmentationNode, segmentID):
    segmentIDs = self.getSegmentIDs(segmentationNode, False)
    return segmentIDs.index(segmentID)

  def getSegmentCentroid(self, segmentationNode, segment):
    imageData = vtkSegmentationCore.vtkOrientedImageData()
    segmentID = segmentationNode.GetSegmentation().GetSegmentIdBySegment(segment)
    self.segmentationsLogic.GetSegmentBinaryLabelmapRepresentation(segmentationNode, segmentID, imageData)
    extent = imageData.GetExtent()
    if extent[1] != -1 and extent[3] != -1 and extent[5] != -1:
      tempLabel = slicer.vtkMRMLLabelMapVolumeNode()
      slicer.mrmlScene.AddNode(tempLabel)
      tempLabel.SetName(segment.GetName() + "CentroidHelper")
      self.segmentationsLogic.CreateLabelmapVolumeFromOrientedImageData(imageData, tempLabel)
      self.applyThreshold(tempLabel, 1)
      centroid = ModuleLogicMixin.getCentroidForLabel(tempLabel, 1)
      slicer.mrmlScene.RemoveNode(tempLabel)
      return centroid
    return None

  def applyThreshold(self, labelNode, outValue):
    imageData = labelNode.GetImageData()
    backgroundValue = 0
    thresh = vtk.vtkImageThreshold()
    thresh.SetInputData(imageData)
    thresh.ThresholdByLower(0)
    thresh.SetInValue(backgroundValue)
    thresh.SetOutValue(outValue)
    thresh.SetOutputScalarType(vtk.VTK_UNSIGNED_CHAR)
    thresh.Update()
    labelNode.SetAndObserveImageData(thresh.GetOutput())

  def calculateSegmentStatistics(self, segNode, grayscaleNode, visibleSegmentsOnly, tableNode=None):
    self.segmentStatisticsLogic.getParameterNode().SetParameter("visibleSegmentsOnly", str(visibleSegmentsOnly))
    self.segmentStatisticsLogic.getParameterNode().SetParameter("Segmentation", segNode.GetID())
    if grayscaleNode:
      self.segmentStatisticsLogic.getParameterNode().SetParameter("ScalarVolume", grayscaleNode.GetID())
    else:
      self.segmentStatisticsLogic.getParameterNode().UnsetParameter("ScalarVolume")
    self.segmentStatisticsLogic.computeStatistics()
    tableNode = self.segmentStatisticsLogic.exportToTable(tableNode)
    return tableNode


class CustomSegmentStatisticsLogic(SegmentStatisticsLogic):

  @staticmethod
  def getDICOMTriplet(codeValue, codingSchemeDesignator, codeMeaning):
    return {'CodeValue':codeValue,
            'CodingSchemeDesignator': codingSchemeDesignator,
            'CodeMeaning': codeMeaning}

  @property
  def statistics(self):
    return self.getStatistics()

  @property
  def segmentationNode(self):
    return slicer.mrmlScene.GetNodeByID(self.getParameterNode().GetParameter("Segmentation"))

  def __init__(self):
    SegmentStatisticsLogic.__init__(self)
    self.terminologyLogic = slicer.modules.terminologies.logic()
    self.segmentationsLogic = slicer.modules.segmentations.logic()

  def exportToTable(self, table=None, nonEmptyKeysOnly=True):
    if not table:
      table = slicer.vtkMRMLTableNode()
      table.SetName(slicer.mrmlScene.GenerateUniqueName(self.grayscaleNode.GetName() + ' statistics'))
      slicer.mrmlScene.AddNode(table)
    table.SetUseColumnNameAsColumnHeader(True)
    SegmentStatisticsLogic.exportToTable(self, table, nonEmptyKeysOnly)
    return table

  def isSegmentValid(self, segmentID):
    for key in self.getNonEmptyKeys():
      if isinstance(self.statistics[segmentID, key], str):
        continue
      if self.statistics[segmentID, key] != 0:
        return True
    return False

  def createJSONFromTerminologyContext(self, terminologyEntry):
    segmentData = dict()

    categoryObject = terminologyEntry.GetCategoryObject()
    if categoryObject is None or not self.isTerminologyInformationValid(categoryObject):
      return {}
    segmentData["SegmentedPropertyCategoryCodeSequence"] = self.getJSONFromVTKSlicerTerminology(categoryObject)

    typeObject = terminologyEntry.GetTypeObject()
    if typeObject is None or not self.isTerminologyInformationValid(typeObject):
      return {}
    segmentData["SegmentedPropertyTypeCodeSequence"] = self.getJSONFromVTKSlicerTerminology(typeObject)

    modifierObject = terminologyEntry.GetTypeModifierObject()
    if modifierObject is not None and self.isTerminologyInformationValid(modifierObject):
      segmentData["SegmentedPropertyTypeModifierCodeSequence"] = self.getJSONFromVTKSlicerTerminology(modifierObject)

    return segmentData

  def createJSONFromAnatomicContext(self, terminologyEntry):
    segmentData = dict()

    regionObject = terminologyEntry.GetAnatomicRegionObject()
    if regionObject is None or not self.isTerminologyInformationValid(regionObject):
      return {}
    segmentData["AnatomicRegionSequence"] = self.getJSONFromVTKSlicerTerminology(regionObject)

    regionModifierObject = terminologyEntry.GetAnatomicRegionModifierObject()
    if regionModifierObject is not None and self.isTerminologyInformationValid(regionModifierObject):
      segmentData["AnatomicRegionModifierSequence"] = self.getJSONFromVTKSlicerTerminology(regionModifierObject)
    return segmentData

  def isTerminologyInformationValid(self, termTypeObject):
    return all(t is not None for t in [termTypeObject.GetCodeValue(), termTypeObject.GetCodingSchemeDesignator(),
                                       termTypeObject.GetCodeMeaning()])

  def getJSONFromVTKSlicerTerminology(self, termTypeObject):
    return self.getDICOMTriplet(termTypeObject.GetCodeValue(), termTypeObject.GetCodingSchemeDesignator(),
                                termTypeObject.GetCodeMeaning())

  def generateJSON4DcmSR(self, dcmSegmentationFile, sourceVolumeNode):
    measurements = []

    sourceImageSeriesUID = ModuleLogicMixin.getDICOMValue(sourceVolumeNode, "0020,000E")
    logging.debug("SourceImageSeriesUID: {}".format(sourceImageSeriesUID))
    segmentationSOPInstanceUID = ModuleLogicMixin.getDICOMValue(dcmSegmentationFile, "0008,0018")
    logging.debug("SegmentationSOPInstanceUID: {}".format(segmentationSOPInstanceUID))

    for segmentID in self.statistics["SegmentIDs"]:
      if not self.isSegmentValid(segmentID):
        continue

      data = dict()
      data["TrackingIdentifier"] = self.statistics[segmentID, "Segment"]
      data["ReferencedSegment"] = len(measurements)+1
      data["SourceSeriesForImageSegmentation"] = sourceImageSeriesUID
      data["segmentationSOPInstanceUID"] = segmentationSOPInstanceUID
      segment = self.segmentationNode.GetSegmentation().GetSegment(segmentID)

      terminologyEntry = self.getDeserializedTerminologyEntry(segment)

      data["Finding"] = self.createJSONFromTerminologyContext(terminologyEntry)["SegmentedPropertyTypeCodeSequence"]
      anatomicContext = self.createJSONFromAnatomicContext(terminologyEntry)
      if anatomicContext.has_key("AnatomicRegionSequence"):
        data["FindingSite"] = anatomicContext["AnatomicRegionSequence"]
      data["measurementItems"] = self.createMeasurementItemsForLabelValue(segmentID)
      measurements.append(data)

    return measurements

  def createMeasurementItemsForLabelValue(self, segmentValue):
    measurementItems = []
    for key in self.getNonEmptyKeys():
      measurementInfo = self.getMeasurementInfo(key)
      if measurementInfo:
        item = dict()
        item["value"] = str(self.statistics[segmentValue, key])
        item["quantity"] = self._createCodeSequence(measurementInfo["DICOM.QuantityCode"])
        item["units"] = self._createCodeSequence(measurementInfo["DICOM.UnitsCode"])
        if measurementInfo.has_key("DICOM.DerivationCode"):
          item["derivationModifier"] = self._createCodeSequence(measurementInfo["DICOM.DerivationCode"])
        measurementItems.append(item)
    return measurementItems

  def _createCodeSequence(self, vtkStringifiedCodedEntry):
    codeSequence = dict()
    for each in vtkStringifiedCodedEntry.split('|'):
      key, value = each.split(":")
      codeSequence[key] = value
    return codeSequence

  def getDeserializedTerminologyEntry(self, segment):
    terminologyWidget = slicer.qSlicerTerminologyNavigatorWidget()
    terminologyEntry = slicer.vtkSlicerTerminologyEntry()
    tag = vtk.mutable("")
    segment.GetTag(segment.GetTerminologyEntryTagName(), tag)
    terminologyWidget.deserializeTerminologyEntry(tag, terminologyEntry)
    return terminologyEntry


class CustomDICOMDetailsWidget(DICOMDetailsWidget, ParameterNodeObservationMixin):

  FinishedLoadingEvent = vtk.vtkCommand.UserEvent + 101

  def __init__(self, dicomBrowser=None, parent=None):
    DICOMDetailsWidget.__init__(self, dicomBrowser, parent)
    self.browserPersistentButton.visible = True
    self.browserPersistentButton.checked = False

  def onLoadingFinished(self):
    DICOMDetailsWidget.onLoadingFinished(self)
    if not self.browserPersistent:
      self.invokeEvent(self.FinishedLoadingEvent)


class TestDataLogic(ScriptedLoadableModuleLogic):
  @staticmethod
  def importIntoDICOMDatabase(dicomFilesDirectory):
    indexer = ctk.ctkDICOMIndexer()
    indexer.addDirectory(slicer.dicomDatabase, dicomFilesDirectory, None)
    indexer.waitForImportFinished()

  @staticmethod
  def unzipSampleData(filePath):
    dicomFilesDirectory = slicer.app.temporaryPath + '/dicomFiles'
    qt.QDir().mkpath(dicomFilesDirectory)
    slicer.app.applicationLogic().Unzip(filePath, dicomFilesDirectory)
    return dicomFilesDirectory

  @staticmethod
  def downloadSampleData():
    import urllib
    downloads = (
      ('http://slicer.kitware.com/midas3/download/item/220834/PieperMRHead.zip', 'PieperMRHead.zip'),
    )
    slicer.util.delayDisplay("Downloading", 1000)
    for url, name in downloads:
      filePath = slicer.app.temporaryPath + '/' + name
      if not os.path.exists(filePath) or os.stat(filePath).st_size == 0:
        slicer.util.delayDisplay('Requesting download %s from %s...\n' % (name, url), 1000)
        urllib.urlretrieve(url, filePath)
    return filePath


class QuantitativeReportingSlicelet(qt.QWidget, ModuleWidgetMixin):

  def __init__(self):
    qt.QWidget.__init__(self)
    self.mainWidget = qt.QWidget()
    self.mainWidget.objectName = "qSlicerAppMainWindow"
    self.mainWidget.setLayout(qt.QHBoxLayout())

    self.setupLayoutWidget()

    self.moduleFrame = qt.QWidget()
    self.moduleFrame.setLayout(qt.QVBoxLayout())
    self.widget = QuantitativeReportingWidget(self.moduleFrame)
    self.widget.setup()

    # TODO: resize self.widget.parent to minimum possible width

    self.scrollArea = qt.QScrollArea()
    self.scrollArea.setWidget(self.widget.parent)
    self.scrollArea.setWidgetResizable(True)
    self.scrollArea.setMinimumWidth(self.widget.parent.minimumSizeHint.width())

    self.splitter = qt.QSplitter()
    self.splitter.setOrientation(qt.Qt.Horizontal)
    self.splitter.addWidget(self.scrollArea)
    self.splitter.addWidget(self.layoutWidget)
    self.splitter.splitterMoved.connect(self.onSplitterMoved)

    self.splitter.setStretchFactor(0,0)
    self.splitter.setStretchFactor(1,1)
    self.splitter.handle(1).installEventFilter(self)

    self.mainWidget.layout().addWidget(self.splitter)
    self.mainWidget.show()

  def setupLayoutWidget(self):
    self.layoutWidget = qt.QWidget()
    self.layoutWidget.setLayout(qt.QHBoxLayout())
    layoutWidget = slicer.qMRMLLayoutWidget()
    layoutManager = slicer.qSlicerLayoutManager()
    layoutManager.setMRMLScene(slicer.mrmlScene)
    layoutManager.setScriptedDisplayableManagerDirectory(slicer.app.slicerHome + "/bin/Python/mrmlDisplayableManager")
    layoutWidget.setLayoutManager(layoutManager)
    slicer.app.setLayoutManager(layoutManager)
    layoutWidget.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
    self.layoutWidget.layout().addWidget(layoutWidget)

  def eventFilter(self, obj, event):
    if event.type() == qt.QEvent.MouseButtonDblClick:
      self.onSplitterClick()

  def onSplitterMoved(self, pos, index):
    vScroll = self.scrollArea.verticalScrollBar()
    print self.moduleFrame.width, self.widget.parent.width, self.scrollArea.width, vScroll.width
    vScrollbarWidth = 4 if not vScroll.isVisible() else vScroll.width + 4 # TODO: find out, what is 4px wide
    if self.scrollArea.minimumWidth != self.widget.parent.minimumSizeHint.width() + vScrollbarWidth:
      self.scrollArea.setMinimumWidth(self.widget.parent.minimumSizeHint.width() + vScrollbarWidth)

  def onSplitterClick(self):
    if self.splitter.sizes()[0] > 0:
      self.splitter.setSizes([0, self.splitter.sizes()[1]])
    else:
      minimumWidth = self.widget.parent.minimumSizeHint.width()
      self.splitter.setSizes([minimumWidth, self.splitter.sizes()[1]-minimumWidth])


if __name__ == "QuantitativeReportingSlicelet":
  import sys
  print( sys.argv )

  slicelet = QuantitativeReportingSlicelet()
