import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import { AppShell } from "../design/AppShell";
import { StatusPill } from "../design/StatusPill";
import { ThemeProvider, themeBootstrapScript } from "../design/theme";
import { ToastProvider } from "../design/Toast";
import { ProjectProvider } from "../features/projects";
import { EventStreamProvider } from "../lib/EventStreamProvider";
import "./globals.css";

// Self-hosted, preloaded, non-render-blocking (replaces the old render-blocking Google
// Fonts @import in globals.css). Same two typefaces the tokens always specified —
// exposed as CSS variables that theme.css's --font-sans/--font-mono compose.
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-jbmono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "OmniAgentOS",
  description: "Mission control for a research lab",
  applicationName: "OmniAgentOS",
  manifest: "/manifest.json",
  icons: {
    icon: [
      { url: "/favicon-32.png", sizes: "32x32", type: "image/png" },
      { url: "/icon.svg", type: "image/svg+xml" },
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  // Installed-app chrome: standalone (no Safari bars), and a status bar that blends
  // into the topbar instead of a black slab above it.
  appleWebApp: {
    capable: true,
    title: "OmniAgentOS",
    statusBarStyle: "default",
  },
  // A console is not a shareable document; keep it out of indexes/link previews.
  robots: { index: false, follow: false },
  formatDetection: { telephone: false },
};

/**
 * Phone-first viewport. `viewportFit: "cover"` is what lets the layout paint under the
 * notch/home indicator — the bottom nav then pays it back with env(safe-area-inset-*)
 * padding. Zoom is NOT capped (maximumScale/userScalable are left alone) because
 * blocking pinch-zoom is an accessibility failure.
 *
 * themeColor is per color-scheme so the browser/OS chrome matches the canvas the boot
 * script is about to stamp: warm paper light, warm ink dark.
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f4f1" },
    { media: "(prefers-color-scheme: dark)", color: "#171613" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${inter.variable} ${jetbrainsMono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootstrapScript }} />
      </head>
      <body>
        <ThemeProvider>
          <ToastProvider>
            {/* ONE EventSource per tab, above every SSE consumer (T1.4). */}
            <EventStreamProvider>
              <ProjectProvider>
                {/* One compact pill replaces the old PauseControl + HealthBadge pair:
                    same two reads (GET /api/pause, GET /api/health) and the same
                    pause.changed subscription, in the prototype's topbar shape. It is
                    read-only — the pause/resume mutation UX lands in a later lane. */}
                <AppShell statusSlot={<StatusPill />}>{children}</AppShell>
              </ProjectProvider>
            </EventStreamProvider>
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
