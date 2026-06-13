import { DOMParser } from '@xmldom/xmldom';
export default {
  createDocument: function createDocument(content) {
    return new DOMParser().parseFromString(content, 'text/xml');
  }
};
//# sourceMappingURL=xml.js.map