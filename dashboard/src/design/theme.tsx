/**
 * Compatibility re-exports — prefer ThemeProvider / ThemeToggle directly.
 * Keeps older imports of `./theme` working during the design-system roll-out.
 */
export {
  ThemeProvider,
  useTheme,
  THEME_BOOT_SCRIPT,
  themeBootstrapScript,
} from "./ThemeProvider";
export { ThemeToggle } from "./ThemeToggle";
