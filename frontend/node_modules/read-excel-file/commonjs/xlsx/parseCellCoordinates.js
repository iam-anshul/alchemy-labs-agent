"use strict";

Object.defineProperty(exports, "__esModule", {
  value: true
});
exports["default"] = parseCellCoordinates;
function _slicedToArray(arr, i) { return _arrayWithHoles(arr) || _iterableToArrayLimit(arr, i) || _unsupportedIterableToArray(arr, i) || _nonIterableRest(); }
function _nonIterableRest() { throw new TypeError("Invalid attempt to destructure non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method."); }
function _unsupportedIterableToArray(o, minLen) { if (!o) return; if (typeof o === "string") return _arrayLikeToArray(o, minLen); var n = Object.prototype.toString.call(o).slice(8, -1); if (n === "Object" && o.constructor) n = o.constructor.name; if (n === "Map" || n === "Set") return Array.from(o); if (n === "Arguments" || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(n)) return _arrayLikeToArray(o, minLen); }
function _arrayLikeToArray(arr, len) { if (len == null || len > arr.length) len = arr.length; for (var i = 0, arr2 = new Array(len); i < len; i++) arr2[i] = arr[i]; return arr2; }
function _iterableToArrayLimit(r, l) { var t = null == r ? null : "undefined" != typeof Symbol && r[Symbol.iterator] || r["@@iterator"]; if (null != t) { var e, n, i, u, a = [], f = !0, o = !1; try { if (i = (t = t.call(r)).next, 0 === l) { if (Object(t) !== t) return; f = !1; } else for (; !(f = (e = i.call(t)).done) && (a.push(e.value), a.length !== l); f = !0); } catch (r) { o = !0, n = r; } finally { try { if (!f && null != t["return"] && (u = t["return"](), Object(u) !== u)) return; } finally { if (o) throw n; } } return a; } }
function _arrayWithHoles(arr) { if (Array.isArray(arr)) return arr; }
function parseCellCoordinates(coordinatesString) {
  // Coordinate examples: "AA2091", "R988", "B1".
  var _coordinatesString$sp = coordinatesString.split(/(\d+)/),
    _coordinatesString$sp2 = _slicedToArray(_coordinatesString$sp, 2),
    column = _coordinatesString$sp2[0],
    row = _coordinatesString$sp2[1];
  return [
  // Row.
  Number(row),
  // Column.
  // It's not clear why would `column` ever be non-trimmed,
  // but if it was added here then perhaps it could hypothetically happen, or smth.
  getColumnNumberFromColumnLetters(column.trim())];
}

// Maps "A1"-like coordinates to `{ row, column }` numeric coordinates.
var LETTERS = ["", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"];

// Converts a letter coordinate to a digit coordinate.
// Examples: "A" -> 1, "B" -> 2, "Z" -> 26, "AA" -> 27, etc.
function getColumnNumberFromColumnLetters(columnLetters) {
  // `for ... of ...` would require Babel polyfill for iterating a string.
  var n = 0;
  var i = 0;
  while (i < columnLetters.length) {
    n *= 26;
    n += LETTERS.indexOf(columnLetters[i]);
    i++;
  }
  return n;
}
//# sourceMappingURL=parseCellCoordinates.js.map