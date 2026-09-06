import { describe, expect, it } from "vitest";
import { hubEventTargets } from "./useAssistantHub";

describe("hubEventTargets", () => {
  it("maps notification events to the notifications source", () => {
    expect(hubEventTargets("notification.created")).toEqual({
      notifications: true,
      proposals: false,
    });
    expect(hubEventTargets("notification.read")).toEqual({
      notifications: true,
      proposals: false,
    });
    expect(hubEventTargets("notification.read_all")).toEqual({
      notifications: true,
      proposals: false,
    });
  });

  it("maps proposal events to the proposals source", () => {
    expect(hubEventTargets("proposal.pending")).toEqual({
      notifications: false,
      proposals: true,
    });
    expect(hubEventTargets("proposal.resolved")).toEqual({
      notifications: false,
      proposals: true,
    });
  });

  it("ignores unrelated event types", () => {
    expect(hubEventTargets("run.started")).toEqual({
      notifications: false,
      proposals: false,
    });
    expect(hubEventTargets("hub.unknown")).toEqual({
      notifications: false,
      proposals: false,
    });
  });
});
