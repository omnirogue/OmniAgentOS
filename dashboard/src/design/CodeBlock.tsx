import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

export type CodeBlockProps = HTMLAttributes<HTMLPreElement> & {
  children: ReactNode;
  /** Cap the block's height and scroll past it (default 28rem). */
  maxHeight?: string;
};

/**
 * A padded, scrollable, mono-font code/output block — the primitive every raw <pre>
 * in the app should use instead of an unstyled browser-default block (run output/input,
 * manifests, diffs). Matches the Experiment cockpit's diff panes exactly.
 */
export function CodeBlock({ children, className, maxHeight, style, ...rest }: CodeBlockProps) {
  return (
    <pre
      className={cx("ds-code-block", className)}
      style={maxHeight ? { maxHeight, ...style } : style}
      {...rest}
    >
      <code>{children}</code>
    </pre>
  );
}
