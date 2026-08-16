import type {
  CompaniesPayload,
  CompaniesPortfolio,
  Company,
  CompanyGoal,
  CompanyView,
  RevenueSource,
  RevenueVertical,
} from "./contracts";

export const BRAND_SLUGS = ["acmeuni", "globex", "initech", "hooli"] as const;
const PLATFORM_SLUG = "omniagentos";

function normalized(value: string): string {
  return value.trim().toLowerCase();
}

/** Revenue source keys are intentionally compact on the API, but retain the
 * vertical before the first colon so the UI can attach health to a card. */
export function sourceVertical(source: RevenueSource): string {
  return normalized(source.key.split(":", 1)[0] ?? "");
}

export function sourceName(source: RevenueSource): string {
  const [, ...rest] = source.key.split(":");
  return rest.join(":") || source.key;
}

export function formatCollectedRoas(roas: number | null): string {
  return roas === null ? "—" : `${roas.toFixed(2)}×`;
}


function goalsFor(company: Company, goals: CompanyGoal[]): CompanyGoal[] {
  return goals
    .filter((goal) => goal.org_company_id === company.id)
    .sort((left, right) => {
      if (left.horizon !== right.horizon) return left.horizon === "long_term" ? -1 : 1;
      return left.title.localeCompare(right.title);
    });
}

function companyView(
  company: Company,
  goals: CompanyGoal[],
  verticals: Map<string, RevenueVertical>,
  sources: RevenueSource[],
): CompanyView {
  const slug = normalized(company.slug);
  return {
    company,
    goals: goalsFor(company, goals),
    vertical: verticals.get(slug) ?? null,
    sources: sources.filter((source) => sourceVertical(source) === slug),
  };
}

/** Projects raw endpoint data into the only visible company surfaces. Personal
 * is deliberately absent from this projection, never an empty placeholder. */
export function buildCompaniesPortfolio(payload: CompaniesPayload): CompaniesPortfolio {
  const verticals = new Map(payload.revenue.verticals.map((vertical) => [normalized(vertical.vertical), vertical]));
  const companiesBySlug = new Map(payload.companies.map((company) => [normalized(company.slug), company]));
  const knownCompanySlugs = new Set(companiesBySlug.keys());

  const brands = BRAND_SLUGS.flatMap((slug) => {
    const company = companiesBySlug.get(slug);
    return company ? [companyView(company, payload.goals, verticals, payload.revenue.sources)] : [];
  });
  const platformCompany = companiesBySlug.get(PLATFORM_SLUG);

  return {
    brands,
    platform: platformCompany ? companyView(platformCompany, payload.goals, verticals, payload.revenue.sources) : null,
    unattributedGlobal: payload.revenue.verticals.find(
      (vertical) => normalized(vertical.vertical) === "global" && !knownCompanySlugs.has(normalized(vertical.vertical)),
    ) ?? null,
    generated_at: payload.revenue.generated_at,
  };
}
