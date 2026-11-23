export interface Schema {
  type: string | string[];
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  enum?: Array<string | number | boolean | null>;
  minimum?: number;
  maximum?: number;
  maxLength?: number;
  maxItems?: number;
  additionalProperties?: false;
}
export function validateSchema(schema: unknown, path?: string): string[];
export function validateValue(schema: Schema, value: unknown, path?: string): string[];
export const boolean: Schema;
export const empty: Schema;
export function string(maxLength?: number): Schema;
export function integer(minimum?: number, maximum?: number): Schema;
export function number(minimum?: number, maximum?: number): Schema;
export function enumeration(...values: Array<string | number | boolean | null>): Schema;
export function array(items: Schema, maxItems?: number): Schema;
export function object(properties: Record<string, Schema>, required?: string[]): Schema;
export function nullable(schema: Schema): Schema;
export function areaMethods(area: string, definitions: Array<[string, string, Schema, Schema, { mutating?: boolean; longOperation?: boolean }?]>): Array<{
  name: string; description: string; mutating: boolean; longOperation: boolean; params: Schema; result: Schema;
}>;
