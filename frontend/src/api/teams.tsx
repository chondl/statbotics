import { APITeamMatch, APITeamYear, APIYear } from "../types/api";
import query, { isLiveYear, version } from "./storage";
import { getTeamYear } from "./team";

export async function getYearTeamYears(
  year: number,
  limit?: number | null
): Promise<{
  year: APIYear;
  team_years: APITeamYear[];
}> {
  let urlSuffix = `/team_years/${year}`;
  let storageKey = `team_years_${year}_${version}`;
  if (limit) {
    urlSuffix += `?limit=${limit}&metric=epa`;
    storageKey += `_${limit}`;
  }
  storageKey += "_v3";

  const live = await isLiveYear(year);
  return query(storageKey, urlSuffix, true, 0, live ? 60 : 60 * 60); // 1 minute / 1 hour
}

export async function getTeamYearTeamMatches(
  year: number,
  teamNum: number
): Promise<APITeamMatch[]> {
  const teamYear = await getTeamYear(teamNum, year);
  return ((teamYear as any)?.team_matches ?? []) as APITeamMatch[];
}
