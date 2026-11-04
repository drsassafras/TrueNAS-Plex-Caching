// The web app's scripts as TypeScript's check reads them (dev/jsconfig.json): the page's own globals, and the types their
// JSDoc names. Nothing here ships: it's what the scripts already are, written down for the check.

// i18n.js and decor.js, and what the server puts in the page (the theme script, the build's data)
declare const PCI18N: any;
declare const PCDECOR: any;
interface Window { PCI18N: any; PCDECOR: any; PCTHEME: any; PCINFO: any; PCBLOCKS: any; }

// The element an event came from or a lookup found, as the scripts read it: a form field, a <details>, a link, or anything
// with data-*. The pages are HTML (the SVG they draw isn't looked up), so a lookup's Element is one of these.
type Field = { closest(selectors: string): Field | null } & HTMLInputElement & HTMLDetailsElement & Pick<HTMLAnchorElement, "href">;
// A handler's event (/** @param {On<MouseEvent>} e */): its target a Field
type On<E extends Event = Event> = E & { target: Field; currentTarget: Field; relatedTarget: Field | null };
interface ParentNode { querySelector(selectors: string): Field | null; querySelectorAll(selectors: string): NodeListOf<Field>; readonly children: HTMLCollectionOf<Field>; }
interface Element { closest(selectors: string): Field | null; }
interface Document { getElementById(elementId: string): Field | null; }

// A row of a card or a table: its fields as each caller fills them
type Row = Record<string, any>;
