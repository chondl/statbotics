import { APITeamMatch } from "../types/api";
import { EventData } from "../types/data";
import query, { version } from "./storage";

export async function getEvent(event: string): Promise<EventData> {
  const urlSuffix = `/event/${event}`;
  const storageKey = `event_${event}_${version}`;

  return query(storageKey, urlSuffix, true, 0, 60); // 1 minute
}

export async function getTeamEventTeamMatches(
  team: number,
  event: string
): Promise<APITeamMatch[]> {
  const eventData = await getEvent(event);
  return ((eventData?.team_matches ?? []) as APITeamMatch[]).filter(
    (tm: any) => tm.team === team
  );
}
