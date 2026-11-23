import type { Schema } from "./_schema.mjs";
export interface MethodDefinition {
  name: string;
  description: string;
  mutating: boolean;
  longOperation: boolean;
  params: Schema;
  result: Schema;
}
export const CONTRACT_VERSION: 1;
export const areas: Readonly<Record<string, MethodDefinition[]>>;
export const methods: readonly MethodDefinition[];
export const methodByName: Map<string, MethodDefinition>;
