import { APIShortEvent, APIShortTeam } from "../types/api";
import query, { version } from "./storage";

// 15 min expiry: these feed the navbar search dropdown, and a long TTL hides
// newly ingested events (an offseason event appearing on event morning was
// invisible in search for up to a week). Both lists are cheap compressed
// blobs (~91KB teams / ~20KB events, <1s). The `_15m` key suffix busts the
// old week-long entries: renamed keys are simply never read again.
export async function getAllTeams(): Promise<APIShortTeam[]> {
  return query(`full_team_list_${version}_15m`, "/teams/all", true, 1000, 60 * 15);
}

export async function getAllEvents(): Promise<APIShortEvent[]> {
  return query(`full_event_list_${version}_15m`, "/events/all", true, 1000, 60 * 15);
}
