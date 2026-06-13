"use strict";

Object.defineProperty(exports, "__esModule", {
  value: true
});
exports["default"] = parseSpreadsheetInfo;
var _xlsx = require("../xml/xlsx.js");
function _createForOfIteratorHelperLoose(o, allowArrayLike) { var it = typeof Symbol !== "undefined" && o[Symbol.iterator] || o["@@iterator"]; if (it) return (it = it.call(o)).next.bind(it); if (Array.isArray(o) || (it = _unsupportedIterableToArray(o)) || allowArrayLike && o && typeof o.length === "number") { if (it) o = it; var i = 0; return function () { if (i >= o.length) return { done: true }; return { done: false, value: o[i++] }; }; } throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method."); }
function _unsupportedIterableToArray(o, minLen) { if (!o) return; if (typeof o === "string") return _arrayLikeToArray(o, minLen); var n = Object.prototype.toString.call(o).slice(8, -1); if (n === "Object" && o.constructor) n = o.constructor.name; if (n === "Map" || n === "Set") return Array.from(o); if (n === "Arguments" || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(n)) return _arrayLikeToArray(o, minLen); }
function _arrayLikeToArray(arr, len) { if (len == null || len > arr.length) len = arr.length; for (var i = 0, arr2 = new Array(len); i < len; i++) arr2[i] = arr[i]; return arr2; }
// I guess `xl/workbook.xml` file should always be present inside the *.xlsx archive.
function parseSpreadsheetInfo(content, xml) {
  var book = xml.createDocument(content);

  // Read `<workbookPr/>` element to detect whether dates are 1900-based or 1904-based.
  // https://support.microsoft.com/en-gb/help/214330/differences-between-the-1900-and-the-1904-date-system-in-excel
  // http://webapp.docx4java.org/OnlineDemo/ecma376/SpreadsheetML/workbookPr.html
  var workbookProperties = (0, _xlsx.getWorkbookProperties)(book);
  var epoch1904 = Boolean(workbookProperties) && workbookProperties.getAttribute('date1904') === '1';

  // Example of `<sheets/>` element:
  //
  // <sheets>
  //   <sheet
  //     xmlns:ns="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  //     name="Sheet1"
  //     sheetId="1"
  //     ns:id="rId3"/>
  // </sheets>

  var sheets = [];
  for (var _iterator = _createForOfIteratorHelperLoose((0, _xlsx.getSheets)(book)), _step; !(_step = _iterator()).done;) {
    var sheet = _step.value;
    if (sheet.getAttribute('name')) {
      sheets.push({
        id: sheet.getAttribute('sheetId'),
        name: sheet.getAttribute('name'),
        relationId: sheet.getAttribute('r:id')
      });
    }
  }
  return {
    epoch1904: epoch1904,
    sheets: sheets
  };
}
//# sourceMappingURL=parseSpreadsheetInfo.js.map