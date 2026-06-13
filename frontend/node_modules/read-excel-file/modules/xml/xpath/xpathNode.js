// This file is no longer used.

import xpath from 'xpath';
export default function (document, element, path) {
  var namespaces = arguments.length > 3 && arguments[3] !== undefined ? arguments[3] : {};
  var select = xpath.useNamespaces(namespaces);
  return select(path, element || document);
}
//# sourceMappingURL=xpathNode.js.map