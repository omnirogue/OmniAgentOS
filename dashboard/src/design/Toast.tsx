"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type ToastTone = "info" | "success" | "error" | "warn" | "ok";

export type ToastItem = {
  id: string;
  title?: string;
  message: string;
  tone?: ToastTone;
  /** Auto-dismiss ms; null/0 = persist */
  duration?: number | null;
  /** Alias used by some call sites */
  durationMs?: number | null;
};

type ToastContextValue = {
  toasts: ToastItem[];
  push: (toast: Omit<ToastItem, "id"> & { id?: string }) => string;
  dismiss: (id: string) => void;
};

const ToastContext = createContext<ToastContextValue | null>(null);

let toastSeq = 0;

function resolveDuration(item: Pick<ToastItem, "duration" | "durationMs">): number | null {
  if (item.durationMs !== undefined) return item.durationMs;
  if (item.duration !== undefined) return item.duration;
  return 4000;
}

function resolveTone(tone?: ToastTone): string {
  if (tone === "ok") return "success";
  return tone ?? "info";
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((toast: Omit<ToastItem, "id"> & { id?: string }) => {
    const id = toast.id ?? `toast-${++toastSeq}`;
    const item: ToastItem = {
      id,
      title: toast.title,
      message: toast.message,
      tone: toast.tone ?? "info",
      duration: resolveDuration(toast),
    };
    setToasts((prev) => [...prev, item]);
    return id;
  }, []);

  const value = useMemo(() => ({ toasts, push, dismiss }), [toasts, push, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}

function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[];
  onDismiss: (id: string) => void;
}) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted || typeof document === "undefined") return null;

  return createPortal(
    <div className="ds-toast-viewport" aria-live="polite" aria-relevant="additions">
      {toasts.map((t) => (
        <ToastCard key={t.id} item={t} onDismiss={onDismiss} />
      ))}
    </div>,
    document.body,
  );
}

function ToastCard({
  item,
  onDismiss,
}: {
  item: ToastItem;
  onDismiss: (id: string) => void;
}) {
  const duration = resolveDuration(item);

  useEffect(() => {
    if (duration == null || duration <= 0) return;
    const id = window.setTimeout(() => onDismiss(item.id), duration);
    return () => window.clearTimeout(id);
  }, [duration, item.id, onDismiss]);

  return (
    <div
      className={cx("ds-toast", `ds-toast--${resolveTone(item.tone)}`)}
      role="status"
    >
      <div className="ds-toast__body">
        {item.title ? <p className="ds-toast__title">{item.title}</p> : null}
        <p className="ds-toast__message">{item.message}</p>
      </div>
      <button
        type="button"
        className="ds-toast__dismiss"
        onClick={() => onDismiss(item.id)}
        aria-label="Dismiss notification"
      >
        <Icon name="close" size={14} />
      </button>
    </div>
  );
}

/** Standalone toast card (for showcase without provider) */
export function Toast({
  title,
  message,
  tone = "info",
  onDismiss,
}: {
  title?: string;
  message: string;
  tone?: ToastTone;
  onDismiss?: () => void;
}) {
  return (
    <div className={cx("ds-toast", `ds-toast--${resolveTone(tone)}`)} role="status">
      <div className="ds-toast__body">
        {title ? <p className="ds-toast__title">{title}</p> : null}
        <p className="ds-toast__message">{message}</p>
      </div>
      {onDismiss ? (
        <button
          type="button"
          className="ds-toast__dismiss"
          onClick={onDismiss}
          aria-label="Dismiss notification"
        >
          <Icon name="close" size={14} />
        </button>
      ) : null}
    </div>
  );
}

/** Controlled stack for demos / custom hosts (not the portal viewport). */
export function ToastStack({
  items,
  onDismiss,
  className,
}: {
  items: ToastItem[];
  onDismiss?: (id: string) => void;
  className?: string;
}) {
  return (
    <div className={cx("ds-toast-stack", className)} style={{ display: "grid", gap: "var(--space-2)" }}>
      {items.map((item) => (
        <Toast
          key={item.id}
          title={item.title}
          message={item.message}
          tone={item.tone}
          onDismiss={onDismiss ? () => onDismiss(item.id) : undefined}
        />
      ))}
    </div>
  );
}
