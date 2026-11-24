import { APIEvent, APIYear } from "../types/api";
import query, { isLiveYear, version } from "./storage";

// eslint-disable-next-line import/prefer-default-export
export async function getYearEvents(year: number): Promise<{ year: APIYear; events: APIEvent[] }> {
  const urlSuffix = `/events/${year}`;
  const storageKey = `events_${year}_${version}`;
  const live = await isLiveYear(year);
  return query(storageKey, urlSuffix, live, 0, live ? 60 : 60 * 60); // 1 minute / 1 hour
}
