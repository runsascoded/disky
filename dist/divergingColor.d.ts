/** Diverging delta colors. Positive is red; negate for green-positive diffs. */
declare function divergingColor(t: number): string;
/** Ink contrasting with a delta fill. */
declare function divergingInk(t: number): string;

export { divergingColor, divergingInk };
