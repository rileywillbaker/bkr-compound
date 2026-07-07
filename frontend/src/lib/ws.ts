// Live feed hook: subscribes to /ws and hands every event to the callback.
// Reconnects with backoff; pings from the server keep proxies from idling out.

import { useEffect, useRef } from "react";

export interface FeedEvent {
  kind: string;
  ts: string;
  payload: Record<string, unknown>;
}

export function useFeed(onEvent: (event: FeedEvent) => void) {
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      ws.onopen = () => {
        attempt = 0;
      };
      ws.onmessage = (msg) => {
        try {
          const event = JSON.parse(msg.data) as FeedEvent;
          if (event.kind !== "ping" && event.kind !== "hello") handler.current(event);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (closed) return;
        attempt += 1;
        timer = setTimeout(connect, Math.min(30_000, 1000 * 2 ** attempt));
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, []);
}
