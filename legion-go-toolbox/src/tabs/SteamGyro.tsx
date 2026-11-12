import { ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useState } from "react";

import { setGameController } from "../backend";
import { replaceSettings, update, useStore } from "../store";
import { ErrorRow, Info, Unavailable } from "../ui";

const LABELS: Record<string, string> = {
  "deck-uhid": "Steam Deck controller",
  xbox: "Xbox controller (no gyro)",
  "ds5-edge": "DualSense Edge",
  ds5: "DualSense",
};
const ORDER = ["deck-uhid", "xbox", "ds5-edge", "ds5"];
const SAME = "__same__";

const label = (t: string, isDefault = false) =>
  (LABELS[t] ?? t) + (isDefault && t === "deck-uhid" ? " (default)" : "");

/** Gyro for ordinary Steam games: make Steam see a controller with a gyro. */
export const SteamGyroSection: FC = () => {
  const { state } = useStore();
  const [busy, setBusy] = useState(false);
  if (!state) return null;
  const { caps, settings, status } = state;
  const c = settings.controls;
  const current = c.steam_controller;
  const games = c.steam_controller_games ?? {};
  const app = status.app;
  const appKey = app.id != null ? String(app.id) : null;
  const gameName = app.name || "This game";
  const gameType = appKey != null ? games[appKey]?.type : undefined;

  const offered = new Set(["xbox", current, ...caps.steam_gyro.types,
    ...Object.values(games).map((g) => g.type)]);
  const types = ORDER.filter((t) => offered.has(t));

  const setGame = async (appId: number, kind: string | null, name: string) => {
    setBusy(true);
    try {
      replaceSettings(await setGameController(appId, kind, name));
    } finally {
      setBusy(false);
    }
  };

  if (!caps.steam_gyro.available && current === "xbox" && Object.keys(games).length === 0) {
    return (
      <PanelSection title="Gyro in Steam games">
        <Unavailable cap={caps.steam_gyro} what="Gyro for Steam games" />
      </PanelSection>
    );
  }

  const active = status.steam_controller;
  const saved = Object.entries(games).sort((a, b) => a[1].name.localeCompare(b[1].name));

  return (
    <PanelSection title="Gyro in Steam games">
      <PanelSectionRow>
        <DropdownItem
          label="All games"
          description={
            current === "xbox"
              ? "Steam only offers gyro for controllers that have one. Pick another type to unlock it."
              : "Set gyro up per game: Steam button → Controller settings → Gyro. Uses the tablet's motion sensor, so it works with the controllers attached."
          }
          rgOptions={types.map((t) => ({ data: t, label: label(t, true) }))}
          selectedOption={current}
          onChange={(o) => update({ controls: { steam_controller: o.data as string } })}
        />
      </PanelSectionRow>

      {app.id != null && (
        <PanelSectionRow>
          <DropdownItem
            label={gameName}
            description="Remembered for this game: it switches when the game starts and back when it closes."
            disabled={busy}
            rgOptions={[
              { data: SAME, label: `Same as all games (${label(current)})` },
              ...types.map((t) => ({ data: t, label: label(t) })),
            ]}
            selectedOption={gameType ?? SAME}
            onChange={(o) =>
              void setGame(app.id as number, o.data === SAME ? null : (o.data as string), gameName)
            }
          />
        </PanelSectionRow>
      )}

      {active && (
        <Info label="Steam sees now">
          {label(active.type, true)}
          {active.source === "game" ? ` – ${gameName}'s own choice` : ""}
        </Info>
      )}
      <ErrorRow message={status.errors.steam_gyro} />

      {saved.length > 0 && (
        <>
          <PanelSectionRow>
            <Field
              label="Games with their own type"
              description="Select one to return it to the all-games type."
              padding="compact"
              bottomSeparator="none"
            />
          </PanelSectionRow>
          {saved.map(([key, g]) => (
            <PanelSectionRow key={key}>
              <ButtonItem
                layout="below"
                disabled={busy}
                description={label(g.type)}
                onClick={() => void setGame(Number(key), null, g.name)}
              >
                {g.name || `App ${key}`}
              </ButtonItem>
            </PanelSectionRow>
          ))}
        </>
      )}

      <PanelSectionRow>
        <Field
          label="Button symbols"
          description="Most games show Xbox symbols whatever type you pick, because Steam passes them an Xbox-style controller. Games with their own PlayStation support may show PlayStation symbols with a DualSense; most of those have a button-prompt option in their settings. Steam's own menus always match the type. The Steam Deck type keeps A/B/X/Y lettering."
          padding="compact"
          bottomSeparator="none"
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
