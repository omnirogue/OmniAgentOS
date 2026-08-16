"use client";

import { useState, type HTMLAttributes } from "react";
import { cx } from "./utils";
import { dataviz } from "./tokens";

export type AvatarSize = "sm" | "md" | "lg";

export type AvatarProps = HTMLAttributes<HTMLSpanElement> & {
  src?: string | null;
  alt?: string;
  name?: string;
  size?: AvatarSize;
};

function initials(name?: string): string {
  if (!name?.trim()) return "?";
  const parts = name.trim().split(/\s+/);
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return `${parts[0]![0] ?? ""}${parts[parts.length - 1]![0] ?? ""}`.toUpperCase();
}

function fallbackColor(name?: string): string {
  const s = name ?? "?";
  let hash = 0;
  for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) | 0;
  const idx = Math.abs(hash) % dataviz.categorical.length;
  return dataviz.categorical[idx]!;
}

export function Avatar({
  src,
  alt,
  name,
  size = "md",
  className,
  style,
  ...rest
}: AvatarProps) {
  const [failed, setFailed] = useState(false);
  const showImg = Boolean(src) && !failed;
  const bg = fallbackColor(name ?? alt);

  return (
    <span
      className={cx("ds-avatar", `ds-avatar--${size}`, className)}
      style={{
        ...style,
        background: showImg ? undefined : `color-mix(in srgb, ${bg} 28%, var(--surface))`,
        color: showImg ? undefined : bg,
        borderColor: showImg ? undefined : `color-mix(in srgb, ${bg} 40%, var(--border))`,
      }}
      role={showImg ? undefined : "img"}
      aria-label={alt ?? name ?? "Avatar"}
      {...rest}
    >
      {showImg ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src!} alt={alt ?? name ?? ""} onError={() => setFailed(true)} />
      ) : (
        <span aria-hidden="true">{initials(name ?? alt)}</span>
      )}
    </span>
  );
}
