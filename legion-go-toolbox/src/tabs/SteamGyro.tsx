import { DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC } from "react";

import { update, useStore } from "../store";
import { ErrorRow, Unavailable } from "../ui";

const LABELS: Record<string, string> = {
  "deck-uhid": "Steam Deck controller (gyro)",
  "ds5-edge": "DualSense Edge (gyro)",
  ds5: "DualSense (gyro)",
};

/** Gyro for ordinary Steam games: make Steam see a controller with a gyro. */
export const SteamGyroSection: FC = () => {
  const { state } = useStore();
  if (!state) return null;
  const { caps, settings, status } = state;
  const current = settings.controls.steam_controller;
  const options = [
    { data: "default", label: "Xbox controller (no gyro)" },
    ...caps.steam_gyro.types.map((t) => ({ data: t, label: LABELS[t] ?? t })),
  ];
  if (!options.some((o) => o.data === current)) {
    options.push({ data: current, label: LABELS[current] ?? current });
  }

  return (
    <PanelSection title="Gyro in Steam games">
      {!caps.steam_gyro.available && current === "default" ? (
        <Unavailable cap={caps.steam_gyro} what="Gyro for Steam games" />
      ) : (
        <>
          <PanelSectionRow>
            <DropdownItem
              label="Steam sees a"
              description={
                current === "default"
                  ? "Steam only offers gyro for controllers that have one. Pick a gyro type to unlock it."
                  : "Now set it up per game: Steam button → Controller settings → Gyro. Uses the motion sensor in the tablet, so it works with the controllers attached."
              }
              rgOptions={options}
              selectedOption={current}
              onChange={(o) => update({ controls: { steam_controller: o.data as string } })}
            />
          </PanelSectionRow>
          <ErrorRow message={status.errors.steam_gyro} />
        </>
      )}
    </PanelSection>
  );
};
