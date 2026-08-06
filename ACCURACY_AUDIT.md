# Neo4j Code Graph — Accuracy Audit

**Generated:** 2026-08-06 16:00:44
**Source root:** `/Users/abhinav/Desktop/Projects/pr-review/final_setup/test`
**Neo4j:** `bolt://localhost:7687` / db `neo4j`

## 12. Overall Graph Statistics

### Node counts by kind

| Labels | Count |
|--------|-------|
| CodeNode, Field | 349,856 |
| CodeNode, Function | 343,001 |
| CodeNode, File | 24,342 |
| CodeNode, Class | 20,212 |
| CodeNode, Package | 2,099 |
| CodeNode, Annotation | 16 |
| CodeNode, External | 11 |
| GraphMeta | 1 |
| CodeNode, Repository | 1 |

### Edge counts by type

| Type | Count |
|------|-------|
| CONTAINS | 739,516 |
| CALLS | 553,540 |
| WRITES | 52,037 |
| INSTANTIATES | 36,598 |
| OVERRIDES | 27,011 |
| EXTENDS | 8,320 |
| OF_TYPE | 7,264 |
| READS | 3,727 |
| ANNOTATED_WITH | 3,546 |
| AUTOWIRED | 1,399 |
| IMPLEMENTS | 1,138 |
| CALLS_EXTERNAL | 20 |

### CALLS breakdown by strategy

| Strategy | Count |
|----------|-------|
| javac_typed | 335,845 |
| name | 76,937 |
| name+arity | 73,307 |
| same_scope+arity | 36,970 |
| receiver_type+arity | 14,605 |
| receiver_type_hint+arity | 13,673 |
| same_scope | 1,159 |
| imports_qualified | 345 |
| bytecode | 243 |
| imports_qualified+arity | 158 |
| receiver_type | 135 |
| imports | 69 |
| same_file+arity | 42 |
| receiver_type_inherited | 20 |
| receiver_type_inherited+arity | 14 |
| same_file | 10 |
| receiver_type_hint_inherited+arity | 8 |

### Repository metadata

- **file_count:** 24342
- **last_indexed_at:** 1786011638.133302
- **updated_at:** 1786011638.133308
- **namespace:** experiment
- **codebase_hash:** 2ac506b6ae3a4607e0d297ace4bbba992ed89db7
- **created_at:** 1786009578.065875
- **edge_count:** 1434096
- **node_count:** 739527
- **status:** ready

### Repository node

- **fqn:** experiment
- **last_indexed:** 1786011632
- **kind:** repository
- **repo:** experiment
- **confidence:** EXTRACTED
- **name:** experiment
- **id:** 4a0aa3d8c6057a32

---

## 1. CALLS edges — `javac_typed` strategy (30 samples)

**Verification:** open caller source file, find method body at reported start/end lines, confirm callee name appears in body.

| # | Mark | Caller | Callee | File | Evidence |
|---|------|--------|--------|------|----------|
| 1 | ✓ | `setGroupByColumns` | `setGroupByColumns` | `XmlExcelReportInheriter.java` | callee `setGroupByColumns` found in body L1735-1739 |
| 2 | ✓ | `constructPickingFormValueBean` | `getScanSku` | `RFOnlineAdvPalletPickProcessHandler.java` | callee `getScanSku` found in body L1497-1563 |
| 3 | ✓ | `closeAllPallets` | `getWorkLineNo` | `Pallet.java` | callee `getWorkLineNo` found in body L1657-1763 |
| 4 | ✓ | `insertErrorDetails` | `getErrorListSize` | `OrderUploadCommonProcess.java` | callee `getErrorListSize` found in body L414-495 |
| 5 | ✓ | `getJQGridResultSet` | `writeLog` | `ArchOrderSpecialInquiryNewServlet.java` | callee `writeLog` found in body L1807-1840 |
| 6 | ✓ | `getPalletId` | `nullCheck` | `RFPickToBatchRequest.java` | callee `nullCheck` found in body L407-410 |
| 7 | ✓ | `doProcess` | `setAccptPoint` | `RFAcceptParametersHandler.java` | callee `setAccptPoint` found in body L104-2164 |
| 8 | ✓ | `service` | `getLineScannerStatus` | `ADAssemblyLineServlet.java` | callee `getLineScannerStatus` found in body L113-818 |
| 9 | ✓ | `validatePalletSameSkuDiffQty` | `setTransmitStatusCd` | `XMLAsnUploadValidator.java` | callee `setTransmitStatusCd` found in body L3488-3571 |
| 10 | ✓ | `doEdiReceivingProcess` | `getSkuUnitsPerPack` | `RFEDIReceivingProcess.java` | callee `getSkuUnitsPerPack` found in body L1663-2089 |
| 11 | ✓ | `getSupplierId` | `nullCheck` | `VendorUploadHdrObject.java` | callee `nullCheck` found in body L1041-1043 |
| 12 | ✓ | `doMasterCartonProcess` | `getNoOfChildCartons` | `RFMasterPalletHdrProcessHandler.java` | callee `getNoOfChildCartons` found in body L118-198 |
| 13 | ✓ | `service` | `getServletContext` | `BillTransAgingReportServlet.java` | callee `getServletContext` found in body L90-191 |
| 14 | ✓ | `getPalletDetails` | `setUserId` | `Edi856AsnUploadProcess.java` | callee `setUserId` found in body L567-1159 |
| 15 | ✓ | `getDynamicFieldsConfig` | `toString` | `DmsDynamicTDCProcessor.java` | callee `toString` found in body L556-578 |
| 16 | ✓ | `doPalletRelease` | `generateEvent` | `ReservationOperations.java` | callee `generateEvent` found in body L395-530 |
| 17 | ✓ | `unFreezePallet` | `getUserId` | `WarehouseFunctions.java` | callee `getUserId` found in body L520-559 |
| 18 | ✓ | `updateSrlNoMaster` | `setOutRefId2` | `SerialNoTracker.java` | callee `setOutRefId2` found in body L2098-2152 |
| 19 | ✓ | `getSkuSplCd3` | `getAttribute` | `RFSkuAtrributeUpdateResponse.java` | callee `getAttribute` found in body L222-225 |
| 20 | ✓ | `updateUplBatchCntl` | `executeUpdateSQL` | `UploadReprocessor.java` | callee `executeUpdateSQL` found in body L227-266 |
| 21 | ✓ | `buildRequestInput` | `getPodId` | `AdhocQueryHighChartHandler.java` | callee `getPodId` found in body L204-366 |
| 22 | ✓ | `gethHideCases` | `getParameter` | `RFCyclecountByLocationRequest.java` | callee `getParameter` found in body L561-564 |
| 23 | ✓ | `getSkuFill` | `writeLog` | `ShipperLite.java` | callee `writeLog` found in body L1077-1105 |
| 24 | ✓ | `validate` | `writeLog` | `EdiCartonObject.java` | callee `writeLog` found in body L162-173 |
| 25 | ✓ | `doDetailProcess` | `constructPickingFormValueBean` | `RFOnlinePickToLightHandler.java` | callee `constructPickingFormValueBean` found in body L270-378 |
| 26 | ✓ | `processWFTask` | `setUniqueSkuFlag` | `GenerateLabelForDemo.java` | callee `setUniqueSkuFlag` found in body L230-633 |
| 27 | ✓ | `insertErrorDetails` | `getOrderDetailObjectList` | `OrderUploadProcess.java` | callee `getOrderDetailObjectList` found in body L482-638 |
| 28 | ✓ | `writeXML` | `insertElement` | `XMLWriter.java` | callee `insertElement` found in body L117-184 |
| 29 | ✓ | `doReplAcceptance` | `palletSku2dbarcodeDecoding` | `AllenRFAcceptance.java` | callee `palletSku2dbarcodeDecoding` found in body L351-1026 |
| 30 | ✓ | `generateCcntSchedule` | `getScheduleNoToGenerate` | `CycleCountScheduler.java` | callee `getScheduleNoToGenerate` found in body L536-575 |

**Result: 30/30 correct (100.0%)** — 0 files not found on disk

---

## 2. CALLS edges — `name` strategy (30 samples)

**Verification:** same as above — confirm callee name in caller method body.

| # | Mark | Caller | Callee | Strategy | File | Evidence |
|---|------|--------|--------|----------|------|----------|
| 1 | ✓ | `companyno_Click` | `toUpperCase` | name | `menuaccessquery.js` | callee found in body L493-577 |
| 2 | ✓ | `getCarrierDesc` | `fetchDataFromDB` | name | `appointmentoperation.js` | callee found in body L398-446 |
| 3 | ✓ | `validateQty` | `parseInt` | name | `rfrecartonize.js` | callee found in body L996-1156 |
| 4 | ✓ | `autoIncrementTakeQty` | `parseInt` | name | `rfbatchpick.js` | callee found in body L1296-1363 |
| 5 | ✓ | `newClick` | `Disable` | name | `slotcontrolhdrdtl.js` | callee found in body L403-547 |
| 6 | ✓ | `showCreateBtn` | `enableButton` | name+arity | `billingrateprofile.js` | callee found in body L456-470 |
| 7 | ✓ | `transDtlClick` | `showPopup` | name+arity | `billchargescancelbyrefid.js` | callee found in body L957-990 |
| 8 | ✓ | `skuClick` | `showSKUPopup` | name+arity | `closeccntdetail.js` | callee found in body L984-1063 |
| 9 | ✓ | `focusColForm` | `Disable` | name | `genericuploadmaintenancedetaillite.js` | callee found in body L1603-1613 |
| 10 | ✓ | `txtPayingOffId_Change` | `toUpperCase` | name | `newcustomer.js` | callee found in body L4873-4898 |
| 11 | ✓ | `viewDetailsClick` | `escape` | name+arity | `tmscustdeliveryinfo.js` | callee found in body L263-307 |
| 12 | ✓ | `cmdPrev_Click` | `submit` | name+arity | `billinginvoicebatchquery.js` | callee found in body L548-563 |
| 13 | ✓ | `carrierChange` | `Enable` | name | `ordergroupcreation.js` | callee found in body L2873-2887 |
| 14 | ✓ | `FetchCustomerDesc` | `fetchDataFromDB` | name | `problempoolcartonrelease.js` | callee found in body L636-689 |
| 15 | ✓ | `_jspService` | `getAllowHostList` | name+arity | `404errorpage.jsp` | callee found in body L1-131 |
| 16 | ✓ | `validateQty` | `parseInt` | name | `orderentrypalletdetail.js` | callee found in body L1907-1948 |
| 17 | ✓ | `routeClick` | `showPopup` | name | `desktoppickconfirmation.js` | callee found in body L1090-1139 |
| 18 | ✓ | `waveClick` | `DispMsg` | name+arity | `trafficreport.js` | callee found in body L663-730 |
| 19 | ✓ | `queryClick` | `submit` | name+arity | `billingmasstranscancel.js` | callee found in body L96-134 |
| 20 | ✓ | `resetClick` | `Enable` | name | `reroutebatchpick.js` | callee found in body L43-78 |
| 21 | ✓ | `getPOPUPCallBack` | `DispMsg` | name+arity | `rmacreditingdemo.js` | callee found in body L3071-3146 |
| 22 | ✓ | `dateClick` | `showDatePicker` | name+arity | `povsinvoice.js` | callee found in body L257-282 |
| 23 | ✓ | `enableGenerateBtn` | `enableButton` | name+arity | `rptinventorypalletcount.js` | callee found in body L161-168 |
| 24 | ✓ | `submitPage` | `submit` | name+arity | `downloadregen.js` | callee found in body L311-322 |
| 25 | ✓ | `tabDataClick` | `Disable` | name | `viewrmadetails.js` | callee found in body L3155-3219 |
| 26 | ✓ | `chkPwrStrip1Click` | `Disable` | name | `rackorderhdrentry.js` | callee found in body L504-518 |
| 27 | ✓ | `deleteClick` | `DispMsg` | name+arity | `notificationhdrinfo.js` | callee found in body L195-232 |
| 28 | ✓ | `rateResetClick` | `Disable` | name | `billingrulerate.js` | callee found in body L1155-1270 |
| 29 | ✓ | `computeBalance` | `parseInt` | name | `desktopbatchdistribution.js` | callee found in body L904-1041 |
| 30 | ✓ | `cmdLogout_Click` | `submit` | name+arity | `billingnewservicelocationselect.js` | callee found in body L50-66 |

**Result: 30/30 correct (100.0%)** — 0 files not found on disk

---

## 3. EXTENDS edges (30 samples)

**Verification:** open child class file, find class declaration line, confirm `extends <ParentName>` appears near it.

| # | Mark | Child | Parent | File | Evidence |
|---|------|-------|--------|------|----------|
| 1 | ✓ | `SkuDimensionUpdateServlet` | `ValidateAccess` | `SkuDimensionUpdateServlet.java` | 'extends ValidateAccess' found near L44 |
| 2 | ✓ | `CountryFactory` | `GenericFactory` | `CountryFactory.java` | 'extends GenericFactory' found near L12 |
| 3 | ✓ | `DbSequenceConfigInfoFactory` | `GenericFactory` | `DbSequenceConfigInfoFactory.java` | 'extends GenericFactory' found near L12 |
| 4 | ✓ | `ZipCodesFactory` | `GenericFactory` | `ZipCodesFactory.java` | 'extends GenericFactory' found near L12 |
| 5 | ✓ | `SchedulerMapping` | `DataControl` | `SchedulerMapping.java` | 'extends DataControl' found near L15 |
| 6 | ✓ | `CcntGen` | `DataControl` | `CcntGen.java` | 'extends DataControl' found near L13 |
| 7 | ✓ | `DummyConfirmCompleteHook` | `AbstractConfirmCompleteHook` | `DummyConfirmCompleteHook.java` | 'extends AbstractConfirmCompleteHook' found near L16 |
| 8 | ✓ | `PTCMessageObject` | `AbstractConcurrencyMessage` | `PTCMessageObject.java` | 'extends AbstractConcurrencyMessage' found near L8 |
| 9 | ✓ | `SuggAisleGrpSeqSkipReplReqWhInvExp` | `DbControl` | `SuggAisleGrpSeqSkipReplReqWhInvExp.java` | 'extends DbControl' found near L61 |
| 10 | ✓ | `TruckroutePathFactory` | `GenericFactory` | `TruckroutePathFactory.java` | 'extends GenericFactory' found near L11 |
| 11 | ✓ | `LblPrintGrpFormatMapFactory` | `GenericFactory` | `LblPrintGrpFormatMapFactory.java` | 'extends GenericFactory' found near L11 |
| 12 | ✓ | `ADTaskManualOperServlet` | `ValidateAccess` | `ADTaskManualOperServlet.java` | 'extends ValidateAccess' found near L58 |
| 13 | ✓ | `PickingProcessCntlServlet` | `ValidateAccess` | `PickingProcessCntlServlet.java` | 'extends ValidateAccess' found near L53 |
| 14 | ✓ | `BRECPSBasicPreProcessHandler` | `BREAbstractPreProcessHandler` | `BRECPSBasicPreProcessHandler.java` | 'extends BREAbstractPreProcessHandler' found near L20 |
| 15 | ✓ | `ExprTemplateGroupMapFactory` | `GenericFactory` | `ExprTemplateGroupMapFactory.java` | 'extends GenericFactory' found near L11 |
| 16 | ✓ | `TableColumnCommentsProcessServlet` | `ValidateAccess` | `TableColumnCommentsProcessServlet.java` | 'extends ValidateAccess' found near L65 |
| 17 | ✓ | `RFCycleCountVerification` | `DbControl` | `RFCycleCountVerification.java` | 'extends DbControl' found near L111 |
| 18 | ✓ | `ReelCodeResponse` | `AbstractScanCodeResponse` | `ReelCodeResponse.java` | 'extends AbstractScanCodeResponse' found near L7 |
| 19 | ✓ | `CycleCountTemplateServlet` | `ValidateAccess` | `CycleCountTemplateServlet.java` | 'extends ValidateAccess' found near L65 |
| 20 | ✓ | `SSOHybridOIDCResponseServlet` | `AppServlet` | `SSOHybridOIDCResponseServlet.java` | 'extends AppServlet' found near L32 |
| 21 | ✓ | `RptAvgReceiptTimeServlet` | `ValidateAccess` | `RptAvgReceiptTimeServlet.java` | 'extends ValidateAccess' found near L68 |
| 22 | ✓ | `SlotZoneDtl` | `DataControl` | `SlotZoneDtl.java` | 'extends DataControl' found near L13 |
| 23 | ✓ | `PerformanceGoalDefn` | `DataControl` | `PerformanceGoalDefn.java` | 'extends DataControl' found near L13 |
| 24 | ✓ | `OrderRelErrorDetailServlet` | `ValidateAccess` | `OrderRelErrorDetailServlet.java` | 'extends ValidateAccess' found near L61 |
| 25 | ✓ | `UnPackingByPCNServlet` | `ValidateAccess` | `UnPackingByPCNServlet.java` | 'extends ValidateAccess' found near L53 |
| 26 | ✓ | `PalletDtlImaging` | `DataControl` | `PalletDtlImaging.java` | 'extends DataControl' found near L14 |
| 27 | ✓ | `RFPickAndDistributeProcessHandler` | `RFProcessHandler` | `RFPickAndDistributeProcessHandler.java` | 'extends RFProcessHandler' found near L92 |
| 28 | ✓ | `CubiscanInboundContentQueue` | `BaseContentQueue` | `CubiscanInboundContentQueue.java` | 'extends BaseContentQueue' found near L7 |
| 29 | ✓ | `WebserviceSetupInfo` | `DataControl` | `WebserviceSetupInfo.java` | 'extends DataControl' found near L9 |
| 30 | ✓ | `AdhocAttributeFiltersFactory` | `GenericFactory` | `AdhocAttributeFiltersFactory.java` | 'extends GenericFactory' found near L11 |

**Result: 30/30 correct (100.0%)** — 0 files not found on disk

---

## 4. IMPLEMENTS edges (20 samples)

**Verification:** open class file, confirm `implements <InterfaceName>` near class declaration line.

| # | Mark | Class | Interface | File | Evidence |
|---|------|-------|-----------|------|----------|
| 1 | ✓ | `TmsPickupTruckTypeDeterminationProcess` | `ITmsHandlerProcess` | `TmsPickupTruckTypeDeterminationProcess.java` | 'implements ITmsHandlerProcess' found near L27 |
| 2 | ✓ | `PageFtr` | `IElement` | `PageFtr.java` | 'implements IElement' found near L48 |
| 3 | ✓ | `WaveCompleteRespMsg` | `ISimulationMessage` | `WaveCompleteRespMsg.java` | 'implements ISimulationMessage' found near L27 |
| 4 | ✓ | `AmsMtoUpload` | `UploadInterface` | `AmsMtoUpload.java` | 'implements UploadInterface' found near L81 |
| 5 | ✓ | `ReportParameterCollection` | `IPersistData` | `ReportParameterCollection.java` | 'implements IPersistData' found near L29 |
| 6 | ✓ | `FTPFilenameHandler` | `IFTPFilenameHandler` | `FTPFilenameHandler.java` | 'implements IFTPFilenameHandler' found near L26 |
| 7 | ✓ | `ReplenishmentTaskCreator` | `TaskGeneratorInterface` | `ReplenishmentTaskCreator.java` | 'implements TaskGeneratorInterface' found near L106 |
| 8 | ✓ | `AbstractWorkFlowHandler` | `IWorkFlow` | `AbstractWorkFlowHandler.java` | 'implements IWorkFlow' found near L29 |
| 9 | ✓ | `WaveInfoMsg` | `ISimulationMessage` | `WaveInfoMsg.java` | 'implements ISimulationMessage' found near L27 |
| 10 | ✓ | `OrderCancellationProcess` | `TemplateDaemonInterface` | `OrderCancellationProcess.java` | 'implements TemplateDaemonInterface' found near L85 |
| 11 | ✓ | `EDI997UploadProcess` | `UploadInterface` | `EDI997UploadProcess.java` | 'implements UploadInterface' found near L33 |
| 12 | ✓ | `ApacheRandomKey` | `RandomKey` | `ApacheRandomKey.java` | 'implements RandomKey' found near L7 |
| 13 | ✓ | `UpdFirstReceiptFlagProcess` | `TemplateDaemonInterface` | `UpdFirstReceiptFlagProcess.java` | 'implements TemplateDaemonInterface' found near L28 |
| 14 | ✓ | `TestModuleErrorWebserviceCall` | `LightModuleErrorServiceProcess` | `TestModuleErrorWebserviceCall.java` | 'implements LightModuleErrorServiceProcess' found near L25 |
| 15 | ✓ | `SonySlotQueryServlet` | `ICustomGridData` | `SonySlotQueryServlet.java` | 'implements ICustomGridData' found near L80 |
| 16 | ✓ | `BaseProperties` | `IProperties` | `BaseProperties.java` | 'implements IProperties' found near L33 |
| 17 | ✓ | `AbstractVoiceProcessor` | `IVoiceProcessor` | `AbstractVoiceProcessor.java` | 'implements IVoiceProcessor' found near L30 |
| 18 | ✓ | `SKUUpcScanDownload` | `DownloadDataInterface` | `SKUUpcScanDownload.java` | 'implements DownloadDataInterface' found near L34 |
| 19 | ✓ | `WaveDescriptionPopulator` | `WaveDescHandler` | `WaveDescriptionPopulator.java` | 'implements WaveDescHandler' found near L34 |
| 20 | ✓ | `BillingOutBoundCancelHandler` | `BillingCancelInterface` | `BillingOutBoundCancelHandler.java` | 'implements BillingCancelInterface' found near L20 |

**Result: 20/20 correct (100.0%)** — 0 files not found on disk

---

## 5. CALLS_EXTERNAL edges (all)

**Verification:** show caller, external node name/fqn, and assess if it represents a real external call (DB driver, HTTP client, etc.).

Total CALLS_EXTERNAL edges: **20**

| # | Caller | Caller File | External Name | External FQN | Strategy | Assessment |
|---|--------|-------------|---------------|--------------|----------|------------|
| 1 | `_jspService` | `fieldlevelsecurity.jsp` | `Connection.close` | `Connection#close` | external_typed | Plausible external call |
| 2 | `_jspService` | `msgpopupnonmodal.jsp` | `Connection.close` | `Connection#close` | external_typed | Plausible external call |
| 3 | `_jspService` | `msgpopupnonmodal.jsp` | `Connection.createStatement` | `Connection#createStatement` | external_typed | Plausible external call |
| 4 | `checkUserMenu` | `fieldlevelsecurity.jsp` | `Connection.prepareStatement` | `Connection#prepareStatement` | external_typed | Plausible external call |
| 5 | `checkMenu` | `fieldlevelsecurity.jsp` | `Connection.prepareStatement` | `Connection#prepareStatement` | external_typed | Plausible external call |
| 6 | `_jspService` | `fieldlevelsecurity.jsp` | `Connection.prepareStatement` | `Connection#prepareStatement` | external_typed | Plausible external call |
| 7 | `_jspService` | `msgpopupnonmodal.jsp` | `Connection.rollback` | `Connection#rollback` | external_typed | Plausible external call |
| 8 | `checkUserMenu` | `fieldlevelsecurity.jsp` | `PreparedStatement.close` | `PreparedStatement#close` | external_typed | Plausible external call |
| 9 | `checkMenu` | `fieldlevelsecurity.jsp` | `PreparedStatement.close` | `PreparedStatement#close` | external_typed | Plausible external call |
| 10 | `_jspService` | `fieldlevelsecurity.jsp` | `PreparedStatement.close` | `PreparedStatement#close` | external_typed | Plausible external call |
| 11 | `checkUserMenu` | `fieldlevelsecurity.jsp` | `PreparedStatement.executeQuery` | `PreparedStatement#executeQuery` | external_typed | Plausible external call |
| 12 | `checkMenu` | `fieldlevelsecurity.jsp` | `PreparedStatement.executeQuery` | `PreparedStatement#executeQuery` | external_typed | Plausible external call |
| 13 | `_jspService` | `fieldlevelsecurity.jsp` | `PreparedStatement.executeQuery` | `PreparedStatement#executeQuery` | external_typed | Plausible external call |
| 14 | `_jspService` | `fieldlevelsecurity.jsp` | `PreparedStatement.setString` | `PreparedStatement#setString` | external_typed | Plausible external call |
| 15 | `checkUserMenu` | `fieldlevelsecurity.jsp` | `ResultSet.close` | `ResultSet#close` | external_typed | Plausible external call |
| 16 | `checkMenu` | `fieldlevelsecurity.jsp` | `ResultSet.close` | `ResultSet#close` | external_typed | Plausible external call |
| 17 | `_jspService` | `msgpopupnonmodal.jsp` | `ResultSet.close` | `ResultSet#close` | external_typed | Plausible external call |
| 18 | `getSession` | `EMail.java` | `Session.getInstance` | `javax.mail.Session#getInstance` | bytecode | Plausible external call |
| 19 | `getSession` | `EMail.java` | `Session.getTransport` | `javax.mail.Session#getTransport` | bytecode | Plausible external call |
| 20 | `_jspService` | `msgpopupnonmodal.jsp` | `Statement.executeQuery` | `Statement#executeQuery` | external_typed | Plausible external call |

**Result: 20/20 plausible external calls**

---

## 6. WRITES edges (20 samples)

**Verification:** open caller source file, confirm field name appears in method body.

| # | Mark | Method | Field | File | Evidence |
|---|------|--------|-------|------|----------|
| 1 | ✓ | `setWaveInfoDisplay` | `waveInfoDisplay` | `PostOrderGroupingProcess.java` | field `waveInfoDisplay` found in body L3235-3238 |
| 2 | ✓ | `SkuSupplierDocMapPK` | `WHSE_ID` | `SkuSupplierDocMapPK.java` | field `WHSE_ID` found in body L31-41 |
| 3 | ✓ | `RplParamManager` | `reorderPercent` | `RplParamManager.java` | field `reorderPercent` found in body L215-282 |
| 4 | ✓ | `setWhseId` | `whseId` | `EDIReceiving.java` | field `whseId` found in body L7298-7301 |
| 5 | ✓ | `setSkuUnitPerCase` | `skuUnitPerCase` | `ValidateBatchBean.java` | field `skuUnitPerCase` found in body L153-155 |
| 6 | ✓ | `setValues` | `eliteGlobObj` | `BillingUploadProcess.java` | field `eliteGlobObj` found in body L147-179 |
| 7 | ✓ | `setValues` | `companyNo` | `EDIInboundInspectionProcess.java` | field `companyNo` found in body L115-136 |
| 8 | ✓ | `RFWoodClearance` | `userId` | `RFWoodClearance.java` | field `userId` found in body L96-112 |
| 9 | ✓ | `setShipperPostalCode` | `shipperPostalCode` | `UPSShipmentMainProcessSurePost.java` | field `shipperPostalCode` found in body L1265-1267 |
| 10 | ✓ | `setDbConn` | `dbConn` | `PostOrderGroupingProcess.java` | field `dbConn` found in body L4294-4297 |
| 11 | ✓ | `setDefBusUnit` | `defBusUnit` | `RptPriceListPdfNew.java` | field `defBusUnit` found in body L843-845 |
| 12 | ✓ | `setFilter3` | `filter3` | `GenericTag.java` | field `filter3` found in body L119-122 |
| 13 | ✓ | `SavePickValidator` | `company` | `SavePickValidator.java` | field `company` found in body L553-715 |
| 14 | ✓ | `setValues` | `prefLocEmptyPartFlag` | `SuggSkuLocationMapping.java` | field `prefLocEmptyPartFlag` found in body L285-311 |
| 15 | ✓ | `CarrExpdInfoPK` | `FROM_ZIP` | `CarrExpdInfoPK.java` | field `FROM_ZIP` found in body L27-39 |
| 16 | ✓ | `execute` | `groupID` | `BillCalendarFortnightlyHookHandler.java` | field `groupID` found in body L61-91 |
| 17 | ✓ | `setValues` | `pBatchId` | `InvHoldReleaseDownloadProcess.java` | field `pBatchId` found in body L88-98 |
| 18 | ✓ | `setCancelResponse` | `cancelResponse` | `USPSVoidShipWSProcess.java` | field `cancelResponse` found in body L500-502 |
| 19 | ✓ | `ShipmentPrefPK` | `PREF_ID3` | `ShipmentPrefPK.java` | field `PREF_ID3` found in body L25-33 |
| 20 | ✓ | `CustCarrLeadTimePK` | `CUST_ID` | `CustCarrLeadTimePK.java` | field `CUST_ID` found in body L23-31 |

**Result: 20/20 correct (100.0%)** — 0 files not found

---

## 6b. READS edges (20 samples)

**Verification:** same — field name should appear in method body.

| # | Mark | Method | Field | File | Evidence |
|---|------|--------|-------|------|----------|
| 1 | ✓ | `doInsertLableParam` | `procId` | `PrintLabelProcessor.java` | field `procId` found in body L577-801 |
| 2 | ✓ | `closeWave` | `errorMesgVect` | `WaveProcessor.java` | field `errorMesgVect` found in body L5083-5291 |
| 3 | ✓ | `getProcType` | `m_ProcType` | `QueueParam.java` | field `m_ProcType` found in body L657-660 |
| 4 | ✓ | `updateToCompleteStatus` | `userId` | `ShipmentDownloadProcess.java` | field `userId` found in body L1708-1757 |
| 5 | ✓ | `doInsertLableParam` | `bldgId` | `PrintLabelProcessor.java` | field `bldgId` found in body L577-801 |
| 6 | ✓ | `getPrefSeqNo` | `m_PrefSeqNo` | `DriverInfo.java` | field `m_PrefSeqNo` found in body L110-113 |
| 7 | ✓ | `isEmpty` | `mHashtable` | `OrderedHashtable.java` | field `mHashtable` found in body L140-143 |
| 8 | ✓ | `getSafetyPercent` | `m_SafetyPercent` | `ZoneControlLimitInfo.java` | field `m_SafetyPercent` found in body L195-198 |
| 9 | ✓ | `isPlannedInvMove` | `isPlannedInvMove` | `PalletPutaway.java` | field `isPlannedInvMove` found in body L6288-6291 |
| 10 | ✓ | `getSearchSql` | `searchSql` | `AutoOrderProcessorOrdSel.java` | field `searchSql` found in body L5784-5787 |
| 11 | ✓ | `workrateRageSummUpdate` | `bldgId` | `ProductivityAnalysisPreProcess.java` | field `bldgId` found in body L1011-1046 |
| 12 | ✓ | `getTaskMgmtId` | `taskId` | `RFPalletReplDeliveryOpt.java` | field `taskId` found in body L4921-4924 |
| 13 | ✓ | `getNoOfCaptionsPerRow` | `noofcaptionperrow` | `XmlExcelReportInheriter.java` | field `noofcaptionperrow` found in body L1879-1881 |
| 14 | ✓ | `setValues` | `userId` | `ReceiptConfirmationDowload.java` | field `userId` found in body L98-132 |
| 15 | ✓ | `getInventoryExceptionsQuery` | `userRequest` | `WmsInventoryTDCProcessor.java` | field `userRequest` found in body L3324-3402 |
| 16 | ✓ | `setValues` | `m_DownloadBatchNo` | `AbstractInvSnapShot846.java` | field `m_DownloadBatchNo` found in body L90-114 |
| 17 | ✓ | `doCloseWaveProcess` | `userRequest` | `AutoCloseWaveProcessor.java` | field `userRequest` found in body L101-176 |
| 18 | ✓ | `write` | `gzipOutputStream` | `GZipResponseStream.java` | field `gzipOutputStream` found in body L51-55 |
| 19 | ✓ | `addPPCN` | `cartMaxQty` | `VariableSizeToteCart.java` | field `cartMaxQty` found in body L88-125 |
| 20 | ✓ | `getLaneAvailabiltyCheck` | `con` | `GenerateLabel.java` | field `con` found in body L969-1019 |

**Result: 20/20 correct (100.0%)** — 0 files not found

---

## 7. INSTANTIATES edges (20 samples)

**Verification:** confirm `new <ClassName>` appears in caller's file body.

| # | Mark | Method | Class | File | Evidence |
|---|------|--------|-------|------|----------|
| 1 | ✓ | `populatePK` | `PtcConfigParamPK` | `PtcConfigParam.java` | `new PtcConfigParamPK` found in body/file |
| 2 | ✓ | `doProcess` | `UpdateProjectSubStatus` | `CreateWorkOrder.java` | `new UpdateProjectSubStatus` found in body/file |
| 3 | ✓ | `service` | `ZeroPickForClusterBatchProcess` | `BatchPickingQueryServlet.java` | `new ZeroPickForClusterBatchProcess` found in body/file |
| 4 | ✓ | `getTemplateDetails` | `MasterDataEntityTemplate` | `AjaxFieldLvlSecurityMasterServlet.java` | `new MasterDataEntityTemplate` found in body/file |
| 5 | ✓ | `printPDFFile` | `PrintPDFFile` | `EndiciaInvoker.java` | `new PrintPDFFile` found in body/file |
| 6 | ✗ | `_jspService` | `RefreshObjectInfo` | `refreshobjectinfo.jsp` | `new RefreshObjectInfo` NOT FOUND in body/file |
| 7 | ✓ | `getFlexResultSet` | `FormBeanObject` | `MassPickConfirmationServlet.java` | `new FormBeanObject` found in body/file |
| 8 | ✓ | `createReceipt` | `FocusObject` | `ManualRMAEntryServlet.java` | `new FocusObject` found in body/file |
| 9 | ✓ | `getTDCResultSet` | `FormBeanObject` | `CartonCancellationServlet.java` | `new FormBeanObject` found in body/file |
| 10 | ✓ | `doInsertInputDemand` | `SimpleDateFormat` | `CIDOperations.java` | `new SimpleDateFormat` found in body/file |
| 11 | ✓ | `getLoggerElements` | `LogElements` | `ParseLoggerXML.java` | `new LogElements` found in body/file |
| 12 | ✓ | `sendMessage` | `PTCRequestObject` | `PTCTestClient.java` | `new PTCRequestObject` found in body/file |
| 13 | ✓ | `doUpdateWithRSetLocCheckDigit` | `Location` | `LocationChkDigitGenerator.java` | `new Location` found in body/file |
| 14 | ✓ | `doCreate` | `BulkDataCopyProcess` | `DOMSCompanyUpload.java` | `new BulkDataCopyProcess` found in body/file |
| 15 | ✓ | `getDispositionSetupObject` | `FieldSpec` | `RFPrePackTypeReceivingProcess.java` | `new FieldSpec` found in body/file |
| 16 | ✗ | `_jspService` | `BillingType` | `billingwhatifanalysis.jsp` | `new BillingType` NOT FOUND in body/file |
| 17 | ✓ | `endOfReport` | `Phrase` | `ItextReportInheriter.java` | `new Phrase` found in body/file |
| 18 | ✓ | `_jspService` | `WebCaptionProvider` | `ordertrackingquery.jsp` | `new WebCaptionProvider` found in body/file |
| 19 | ✓ | `updateBinLogACK` | `FieldSpec` | `PTCLoggerUtil.java` | `new FieldSpec` found in body/file |
| 20 | ✓ | `generateLabelAndPrint` | `AppException` | `LabelGenerator.java` | `new AppException` found in body/file |

**Result: 18/20 correct (90.0%)** — 0 files not found

---

## 8. OVERRIDES edges (20 samples)

**Verification:** for each child method, open child class file, confirm method is defined there AND check for `@Override` annotation or matching parent method signature.

| # | Mark | Child Method | Parent Method | Child File | Evidence |
|---|------|-------------|---------------|------------|----------|
| 1 | ✓ | `populatePK` | `populatePK` | `ConfigData.java` | method found at L217; same name |
| 2 | ✓ | `service` | `service` | `ShipmentCostDetailServlet.java` | method found at L60; same name |
| 3 | ✓ | `hasOwnMail` | `hasOwnMail` | `AbstractDatabaseServer.java` | method found at L156; same name |
| 4 | ✓ | `execute` | `execute` | `TruckLeaveServlet.java` | method found at L372; same name |
| 5 | ✓ | `getPK` | `getPK` | `OrderReleaseInstance.java` | method found at L876; same name |
| 6 | ✓ | `service` | `service` | `UploadGenericDtlServlet.java` | method found at L42; same name |
| 7 | ✓ | `service` | `service` | `RptKeyPerformanceIndServlet.java` | method found at L102; same name |
| 8 | ✓ | `service` | `service` | `MobileUtilityServlet.java` | method found at L51; same name |
| 9 | ✓ | `writeLog` | `writeLog` | `EmployeeProfileServlet.java` | method found at L804; same name |
| 10 | ✓ | `execute` | `execute` | `PackingStationSummaryServlet.java` | method found at L878; same name |
| 11 | ✓ | `doProcess` | `doProcess` | `Edi945ShipAdviceProcess.java` | method found at L122; same name |
| 12 | ✓ | `doProcess` | `doProcess` | `ADDlogUploadProcess.java` | method found at L122; same name |
| 13 | ✓ | `execute` | `execute` | `ViewStgLocPalletAdvanceServlet.java` | method found at L708; same name |
| 14 | ✓ | `setUserRequest` | `setUserRequest` | `BaseProperties.java` | method found at L263; has @Override |
| 15 | ✓ | `service` | `service` | `GenericUploadTempIntegrityCopyServlet.java` | method found at L39; same name |
| 16 | ✓ | `getTDCResultSet` | `getTDCResultSet` | `DHLCourierReportServlet.java` | method found at L500; same name |
| 17 | ✓ | `execute` | `execute` | `ViewTruckSPNDetailServlet.java` | method found at L898; same name |
| 18 | ✓ | `execute` | `execute` | `ReleaseShortPickedOrdGrpServlet.java` | method found at L679; same name |
| 19 | ✓ | `validatePalletId` | `validatePalletId` | `Edi856AsnUploadValidator.java` | method found at L198; same name |
| 20 | ✓ | `doInitProcess` | `doInitProcess` | `BillingAllenReturnsHandlerBySkuGrp.java` | method found at L125; same name |

**Result: 20/20 correct (100.0%)** — 0 files not found

---

## 9. Coverage Gaps — 5 Java File Spot-Checks

For each file, list the method calls that appear in the source and check whether they appear as CALLS edges in the graph.

### File: `src/main/java/com/softeon/scm/app/objects/RetReceiptHdr.java`

Top method-call names found in source:

| Call Name | In Graph? |
|-----------|----------|
| `containsKey` | ✗ missing |
| `get` | ✗ missing |
| `currentTimeMillis` | ✗ missing |
| `intValue` | ✗ missing |
| `getWhseId` | ✗ missing |
| `getBldgId` | ✗ missing |
| `getCompanyNo` | ✗ missing |
| `getRmaNo` | ✗ missing |
| `getRetReceiptNo` | ✗ missing |
| `getReceiptDate` | ✗ missing |

Graph coverage of top-10 calls: **0/10**

### File: `src/main/java/com/softeon/scm/app/objects/AddressPK.java`

Top method-call names found in source:

| Call Name | In Graph? |
|-----------|----------|
| `getAddrId` | ✗ missing |

Graph coverage of top-10 calls: **0/10**

### File: `src/main/java/com/softeon/scm/dms/picking/PutToWallClusterSavePickProcessor.java`

Top method-call names found in source:

| Call Name | In Graph? |
|-----------|----------|
| `append` | ✗ missing |
| `writeLog` | ✓ yes |
| `nullCheck` | ✗ missing |
| `put` | ✗ missing |
| `getStringArray` | ✗ missing |
| `addElement` | ✗ missing |
| `elementAt` | ✗ missing |
| `get` | ✗ missing |
| `add` | ✗ missing |
| `toString` | ✗ missing |

Graph coverage of top-10 calls: **1/10**

### File: `src/main/java/scm/dms/queries/ViewMultipleTrucksServlet.java`

Top method-call names found in source:

| Call Name | In Graph? |
|-----------|----------|
| `setAttribute` | ✗ missing |
| `nullCheck` | ✗ missing |
| `getAttribute` | ✗ missing |
| `trim` | ✗ missing |
| `getParameter` | ✗ missing |
| `length` | ✗ missing |
| `getInteger` | ✗ missing |
| `writeLog` | ✓ yes |
| `addElement` | ✗ missing |
| `getStringArray` | ✗ missing |

Graph coverage of top-10 calls: **1/10**

### File: `src/main/java/com/softeon/scm/impl/ad/luca/process/PrintAndApplyListenerObj.java`

Top method-call names found in source:

| Call Name | In Graph? |
|-----------|----------|
| `writeLog` | ✓ yes |
| `shutdown` | ✗ missing |
| `newSingleThreadExecutor` | ✗ missing |
| `getPrinterId` | ✗ missing |
| `getThreadPool` | ✗ missing |
| `getService` | ✗ missing |
| `log` | ✗ missing |

Graph coverage of top-10 calls: **1/10**

---

## 10. False Positive Check — Heuristic CALLS (name strategy)

For each sampled heuristic CALLS edge, check whether the callee's containing class is imported or referenced in the caller's file. If not, the edge may target the wrong method.

| # | Caller | Callee | Callee Class | Callee Class imported? | Risk |
|---|--------|--------|--------------|------------------------|------|
| 1 | `validateFields` | `isDate` | `?` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 2 | `validateUnitSrlNo` | `parseInt` | `TableData` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 3 | `printClick` | `parseInt` | `TableData` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 4 | `_jspService` | `format` | `SQLParser` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 5 | `getChargeCodeDesc` | `fetchDataFromDB` | `?` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 6 | `nextClick` | `submit` | `RplParamManager` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 7 | `getBldgDescription` | `fetchDataFromDB` | `VirtualPopupAJAXHandler` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 8 | `cmdReset_Click` | `Enable` | `?` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 9 | `queryClick` | `$` | `?` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |
| 10 | `companyNoClick` | `DispMsg` | `?` | ✗ | **HIGH** — callee class not found in caller file (possible wrong target) |

**High-risk false positives: 10/10**

---

## 11. Node Accuracy — Function start_line / end_line (10 samples)

**Verification:** open source file, check that the line at `start_line` contains the method signature (method name), and that `end_line` is after `start_line` and plausibly the closing `}`.

| # | Mark | Method | File | Start | End | Evidence |
|---|------|--------|------|-------|-----|----------|
| 1 | ✓ | `checkPasswordCharExists` | `ResetPasswordServlet.java` | 1064 | 1120 | L1064: sig found; L1120: } found (file has 1230 lines) |
| 2 | ✓ | `getSkuGroupInvQuery` | `InventoryAllocation.java` | 1314 | 1332 | L1314: sig found; L1332: } found (file has 1471 lines) |
| 3 | ✓ | `checkClick` | `zonezonegrpsetup.js` | 716 | 720 | L716: sig found; L720: } found (file has 744 lines) |
| 4 | ✓ | `getAdjacentNodeCallBack` | `slothtmllayout.js` | 857 | 908 | L857: sig found; L908: } found (file has 1831 lines) |
| 5 | ✓ | `getToMailId` | `BillingProfileNotificationProcess.java` | 310 | 313 | L310: sig found; L313: } found (file has 345 lines) |
| 6 | ✓ | `clickOnLink` | `ftiens4.js` | 697 | 702 | L697: sig found; L702: } found (file has 1199 lines) |
| 7 | ✓ | `submitClick` | `tmszone.js` | 125 | 187 | L125: sig found; L187: } found (file has 271 lines) |
| 8 | ✓ | `enableAll` | `carrierzonemap.js` | 27 | 37 | L27: sig found; L37: } found (file has 881 lines) |
| 9 | ✓ | `getInterwhseTransferAisle` | `WmsPopupQuery.java` | 6589 | 6596 | L6589: sig found; L6596: } found (file has 16102 lines) |
| 10 | ✓ | `getHoldSource` | `PalletHoldOperations.java` | 3142 | 3144 | L3142: sig found; L3144: } found (file has 3176 lines) |

**Result: 10/10 correct (100.0%)** — 0 files not found

---

## Summary

### Accuracy by edge type

| Edge Type | Verified | Correct | Accuracy | Notes |
|-----------|----------|---------|----------|-------|
| CALLS (javac_typed) | 30 | 30 | 100.0% | Highest-confidence strategy |
| CALLS (name*) | 30 | 30 | 100.0% | Heuristic; lower confidence |
| EXTENDS | 30 | 30 | 100.0% | |
| IMPLEMENTS | 20 | 20 | 100.0% | |
| CALLS_EXTERNAL | 20 | 20 | 100.0% | All edges listed |
| WRITES | 20 | 20 | 100.0% | |
| READS | 20 | 20 | 100.0% | |
| INSTANTIATES | 20 | 18 | 90.0% | |
| OVERRIDES | 20 | 20 | 100.0% | |
| Function node lines | 10 | 10 | 100.0% | start_line+end_line accuracy |

### False positive risk (heuristic CALLS)

- High-risk false positives in name-strategy sample: **10/10**
  - These are edges where the callee's class is not referenced/imported in the caller's file
  - Recommendation: treat `name`-strategy edges with caution; prefer `javac_typed` for analysis

### Notable issues / recommendations

1. **javac_typed edges** are the most reliable — verify those first for any analysis task.
2. **name-strategy** heuristics can produce false positives when method names are common across multiple classes.
3. **CALLS_EXTERNAL** only has 20 edges total — the graph may be under-capturing external library calls.
4. **WRITES/READS** field-access edges should be interpreted carefully for dynamically named fields.
5. **Coverage gaps** (Section 9) show whether the graph is missing common method calls — check those results for per-file gap rates.
6. **Node line numbers** (Section 11) — if accuracy < 90%, source offsets are unreliable; use with caution in UI navigation.
7. **OVERRIDES** — @Override annotations may not always be present in legacy Java code; the heuristic relies on same-name + class hierarchy.

---
*Audit completed 2026-08-06 16:00:47*
