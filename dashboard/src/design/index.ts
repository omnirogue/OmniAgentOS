/** OmniAgentOS design system — primitives + chart kit. */

export { cx, cssVar, chartSeriesColors, sequentialColor } from "./utils";

export {
  font,
  space,
  radius,
  shadow,
  motion,
  color,
  dataviz,
  zIndex,
  layout,
  semantic,
  CHART_KIT,
} from "./tokens";
export type { ThemeName } from "./tokens";

export {
  ThemeProvider,
  useTheme,
  themeBootstrapScript,
  THEME_BOOT_SCRIPT,
} from "./ThemeProvider";
export { ThemeToggle } from "./ThemeToggle";
export { AppShell } from "./AppShell";
export type { AppShellProps } from "./AppShell";

export { BottomNav, BOTTOM_NAV_ITEMS, bottomNavActiveKey } from "./BottomNav";
export type { BottomNavProps, BottomNavKey } from "./BottomNav";

export { StatusPill, statusPillView, relativeSince } from "./StatusPill";
export type { StatusPillTone, StatusPillView } from "./StatusPill";

export { Icon } from "./Icon";
export type { IconName, IconProps } from "./Icon";

export { Page } from "./Page";
export type { PageProps } from "./Page";

export { PageHeader } from "./PageHeader";
export type { PageHeaderProps } from "./PageHeader";

export { Section } from "./Section";
export type { SectionProps } from "./Section";

export { CodeBlock } from "./CodeBlock";
export type { CodeBlockProps } from "./CodeBlock";

export { DefinitionList } from "./DefinitionList";
export type { DefinitionItem, DefinitionListProps } from "./DefinitionList";

export { Button } from "./Button";
export type { ButtonProps, ButtonVariant, ButtonSize } from "./Button";

export { Card } from "./Card";
export type { CardProps, CardPadding } from "./Card";

export { Badge } from "./Badge";
export type { BadgeProps, BadgeTone, BadgeCategory } from "./Badge";

export { Table, TableRoot, Th } from "./Table";
export type { TableProps, TableColumn, SortDir } from "./Table";

export { Stat } from "./Stat";
export type { StatProps, StatTone } from "./Stat";

export { Tabs } from "./Tabs";
export type { TabsProps, TabItem } from "./Tabs";

export { Dialog } from "./Dialog";
export type { DialogProps } from "./Dialog";

export { Toast, ToastStack, ToastProvider, useToast } from "./Toast";
export type { ToastItem, ToastTone } from "./Toast";

export { Tooltip } from "./Tooltip";
export type { TooltipProps } from "./Tooltip";

export { Input } from "./Input";
export type { InputProps } from "./Input";

export { Select } from "./Select";
export type { SelectProps, SelectOption } from "./Select";

export { Pill } from "./Pill";
export type { PillProps, PillTone } from "./Pill";

export { Avatar } from "./Avatar";
export type { AvatarProps, AvatarSize } from "./Avatar";

export { StatusDot } from "./StatusDot";
export type { StatusDotProps, StatusDotState } from "./StatusDot";

export { EmptyState } from "./EmptyState";
export type { EmptyStateProps } from "./EmptyState";

export { Loading } from "./Loading";
export type { LoadingProps } from "./Loading";

export { ErrorState } from "./ErrorState";
export type { ErrorStateProps } from "./ErrorState";

export { ErrorBoundary } from "./ErrorBoundary";
export type { ErrorBoundaryProps } from "./ErrorBoundary";

export {
  CommandPalette,
  CommandPaletteHost,
  CmdKButton,
  openCommandPalette,
  DEFAULT_COMMANDS,
  SCOPED_COMMANDS,
} from "./CommandPalette";
export type { CommandItem, CommandPaletteProps } from "./CommandPalette";

export * as charts from "./charts";
export {
  LineChart,
  BarChart,
  Bracket,
  Sparkline,
  Empty as ChartEmpty,
  Loading as ChartLoading,
  ErrorState as ChartErrorState,
  Tooltip as ChartTooltip,
} from "./charts";
export type {
  LineChartProps,
  LinePoint,
  LineSeries,
  BarChartProps,
  BarItem,
  BracketProps,
  Match,
  SparklineProps,
} from "./charts";
