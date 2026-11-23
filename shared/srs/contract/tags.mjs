import { areaMethods, array, boolean as b, empty, object as o, string as s } from "./_schema.mjs";
import { mutation, operation } from "./_shapes.mjs";
export default areaMethods("tags", [
  ["list", "Read hierarchical tags with distinct subtree note counts and native collapse state in the terminal progress result; this operation does not mutate the collection.", o({ prefix: s(1024) }, []), operation, { mutating: false, longOperation: true }],
  ["rename", "Rename a tag and optionally its descendants on all notes; the terminal progress result includes the changed note count.", o({ name: s(1024), newName: s(1024), includeChildren: b }), operation, { mutating: true, longOperation: true }],
  ["reparent", "Move tags under a hierarchical parent; the terminal progress result includes the changed note count.", o({ names: array(s(1024), 1000), parent: s(1024) }), operation, { mutating: true, longOperation: true }],
  ["remove", "Remove tags from notes and the tag registry in one native commit; the terminal progress result includes the changed note count. Exact mixed removal of retained parents and fully selected subtrees is unsupported before writing: remove those categories in two separate includeChildren:false requests, which are not jointly atomic.", o({ names: array(s(1024), 1000), includeChildren: b }), operation, { mutating: true, longOperation: true }],
  ["clearUnused", "Clear registered tags no longer used by notes; the terminal progress result includes the removed tag count.", empty, operation, { mutating: true, longOperation: true }],
  ["setCollapsed", "Persist a tag tree's expanded state.", o({ name: s(1024), collapsed: b }), mutation(), { mutating: true, longOperation: false }],
]);
