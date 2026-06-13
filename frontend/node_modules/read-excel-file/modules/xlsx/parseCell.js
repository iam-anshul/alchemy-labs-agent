import parseCellValue from './parseCellValue.js';
import parseCellCoordinates from './parseCellCoordinates.js';
import { getCellValueElement, getCellInlineStringValue } from '../xml/xlsx.js';
import { getOuterXml } from '../xml/dom.js';

// Example of a `<c/>`ell element:
//
// <c>
//    <f>string</f> — formula.
//    <v>string</v> — formula pre-computed value.
//    <is>
//       <t>string</t> — an `inlineStr` string (rather than a "common string" from a dictionary).
//       <r>
//          <rPr>
//            ...
//          </rPr>
//          <t>string</t>
//       </r>
//       <rPh sb="1" eb="1">
//          <t>string</t>
//       </rPh>
//       <phoneticPr fontId="1"/>
//    </is>
//    <extLst>
//       <ext>
//          <!--any element-->
//       </ext>
//    </extLst>
// </c>
//
export default function parseCell(element, sheetDocument, sharedStrings, styles, epoch1904, options) {
  var coordinates = parseCellCoordinates(element.getAttribute('r'));
  var valueElement = getCellValueElement(sheetDocument, element);

  // When the value element doesn't exist, it would be returned as `undefined`
  // when using `xpath` and as `null` when using `DOMParser`.
  // So here it uses `value && ...` syntax instead of `if (value !== undefined) { ... }`
  // for compatibility with both `xpath` and `DOMParser`.
  var value = valueElement && valueElement.textContent;
  var type = element.getAttribute('t');
  return {
    row: coordinates[0],
    column: coordinates[1],
    value: parseCellValue(value, type, {
      getInlineStringValue: function getInlineStringValue() {
        return getCellInlineStringValue(sheetDocument, element);
      },
      getInlineStringXml: function getInlineStringXml() {
        return getOuterXml(element);
      },
      getStyleId: function getStyleId() {
        return element.getAttribute('s');
      },
      styles: styles,
      sharedStrings: sharedStrings,
      epoch1904: epoch1904,
      options: options
    })
  };
}
//# sourceMappingURL=parseCell.js.map