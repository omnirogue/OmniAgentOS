export interface AnsiSpan {
  text: string;
  bold?: boolean;
  color?: string;
  bgColor?: string;
}

const COLOR_MAP: Record<string, string> = {
  black: "#1f2937",
  red: "#f87171",
  green: "#4ade80",
  yellow: "#facc15",
  blue: "#60a5fa",
  magenta: "#c084fc",
  cyan: "#22d3ee",
  white: "#f3f4f6",
  "bright-black": "#4b5563",
  "bright-red": "#ef4444",
  "bright-green": "#22c55e",
  "bright-yellow": "#eab308",
  "bright-blue": "#3b82f6",
  "bright-magenta": "#a855f7",
  "bright-cyan": "#06b6d4",
  "bright-white": "#ffffff",
};

export function parseAnsi(text: string): AnsiSpan[] {
  const spans: AnsiSpan[] = [];
  // Matches ESC[...m
  const ansiRegex = /\x1b\[([0-9;]*)m/g;
  
  let match;
  let lastIndex = 0;
  
  let currentBold = false;
  let currentColor: string | undefined = undefined;
  let currentBgColor: string | undefined = undefined;
  
  const colors: Record<string, string> = {
    "30": "black",
    "31": "red",
    "32": "green",
    "33": "yellow",
    "34": "blue",
    "35": "magenta",
    "36": "cyan",
    "37": "white",
    "90": "bright-black",
    "91": "bright-red",
    "92": "bright-green",
    "93": "bright-yellow",
    "94": "bright-blue",
    "95": "bright-magenta",
    "96": "bright-cyan",
    "97": "bright-white",
  };
  
  const bgColors: Record<string, string> = {
    "40": "black",
    "41": "red",
    "42": "green",
    "43": "yellow",
    "44": "blue",
    "45": "magenta",
    "46": "cyan",
    "47": "white",
    "100": "bright-black",
    "101": "bright-red",
    "102": "bright-green",
    "103": "bright-yellow",
    "104": "bright-blue",
    "105": "bright-magenta",
    "106": "bright-cyan",
    "107": "bright-white",
  };

  while ((match = ansiRegex.exec(text)) !== null) {
    const textPart = text.slice(lastIndex, match.index);
    if (textPart) {
      spans.push({
        text: textPart,
        bold: currentBold,
        color: currentColor ? COLOR_MAP[currentColor] : undefined,
        bgColor: currentBgColor ? COLOR_MAP[currentBgColor] : undefined,
      });
    }
    
    const codes = match[1].split(";");
    for (const code of codes) {
      const normalized = code === "" ? "0" : code;
      if (normalized === "0") {
        currentBold = false;
        currentColor = undefined;
        currentBgColor = undefined;
      } else if (normalized === "1") {
        currentBold = true;
      } else if (colors[normalized]) {
        currentColor = colors[normalized];
      } else if (bgColors[normalized]) {
        currentBgColor = bgColors[normalized];
      }
    }
    
    lastIndex = ansiRegex.lastIndex;
  }
  
  const remainingText = text.slice(lastIndex);
  if (remainingText) {
    spans.push({
      text: remainingText,
      bold: currentBold,
      color: currentColor ? COLOR_MAP[currentColor] : undefined,
      bgColor: currentBgColor ? COLOR_MAP[currentBgColor] : undefined,
    });
  }
  
  return spans;
}
