"use strict";

Object.defineProperty(exports, "__esModule", {
  value: true
});
exports["default"] = parseSheet;
var _parseCells = _interopRequireDefault(require("./parseCells.js"));
var _parseSheetDimensions = _interopRequireDefault(require("./parseSheetDimensions.js"));
var _reconstructSheetDimensionsFromSheetCells = _interopRequireDefault(require("./reconstructSheetDimensionsFromSheetCells.js"));
var _convertCellsToData2dArray = _interopRequireDefault(require("./convertCellsToData2dArray.js"));
function _interopRequireDefault(obj) { return obj && obj.__esModule ? obj : { "default": obj }; }
function parseSheet(content, xml, sharedStrings, styles, epoch1904, options) {
  var sheetDocument = xml.createDocument(content);
  var cells = (0, _parseCells["default"])(sheetDocument, sharedStrings, styles, epoch1904, options);

  // `dimensions` defines the spreadsheet area enclosing all non-empty cells.
  // https://docs.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.sheetdimension?view=openxml-2.8.1
  var dimensions = (0, _parseSheetDimensions["default"])(sheetDocument) || (0, _reconstructSheetDimensionsFromSheetCells["default"])(cells);
  return (0, _convertCellsToData2dArray["default"])(cells, dimensions);
}
//# sourceMappingURL=parseSheet.js.map