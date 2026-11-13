import { api } from "./api";

// Times are shown in the timezone the assistant is configured for, which can differ from
// this device's while travelling.
let zone: string | undefined;

export async function loadZone(): Promise<void> {
  if (!zone) zone = (await api.settings()).timezone;
}

export function stamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: zone,
  });
}
