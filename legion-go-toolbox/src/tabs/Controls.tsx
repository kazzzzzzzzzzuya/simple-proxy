import { DropdownItem, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { FC } from "react";

import type { RemapButton } from "../backend";
import { update, useStore } from "../store";
import { ErrorRow, Unavailable } from "../ui";

const BUTTONS: { key: RemapButton; label: string }[] = [
  { key: "Y1", label: "Y1 (left back, upper)" },
  { key: "Y2", label: "Y2 (left back, lower)" },
  { key: "Y3", label: "Y3 (right back)" },
  { key: "M2", label: "M2 (right side, upper)" },
  { key: "M3", label: "M3 (right side, lower)" },
];

const LAYOUTS = [
  { data: "steam_menu", label: "Steam ↔ Menu, QAM ↔ View (default)" },
  { data: "steam_view", label: "Steam ↔ View, QAM ↔ Menu" },
];

const ACTIONS: { data: string; label: string }[] = [
  { data: "none", label: "No action" },
  { data: "a", label: "A" },
  { data: "b", label: "B" },
  { data: "x", label: "X" },
  { data: "y", label: "Y" },
  { data: "lb", label: "LB" },
  { data: "rb", label: "RB" },
  { data: "lt", label: "LT" },
  { data: "rt", label: "RT" },
  { data: "view", label: "View" },
  { data: "menu", label: "Menu" },
  { data: "dpad_up", label: "D-pad up" },
  { data: "dpad_down", label: "D-pad down" },
  { data: "dpad_left", label: "D-pad left" },
  { data: "dpad_right", label: "D-pad right" },
  { data: "ls_click", label: "Left stick click" },
  { data: "rs_click", label: "Right stick click" },
  { data: "ls_up", label: "Left stick up" },
  { data: "ls_down", label: "Left stick down" },
  { data: "ls_left", label: "Left stick left" },
  { data: "ls_right", label: "Left stick right" },
  { data: "rs_up", label: "Right stick up" },
  { data: "rs_down", label: "Right stick down" },
  { data: "rs_left", label: "Right stick left" },
  { data: "rs_right", label: "Right stick right" },
];

export const ControlsTab: FC = () => {
  const { state } = useStore();
  if (!state) return null;
  const { caps, settings, status } = state;
  const c = settings.controls;

  return (
    <>
      <PanelSection title="Menu buttons">
        {!caps.swap_menu.available && !c.swap_menu ? (
          <Unavailable cap={caps.swap_menu} what="Swapping the menu buttons" />
        ) : (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Swap menu buttons"
                description={
                  c.swap_layout === "steam_view"
                    ? "View opens Steam and Menu opens Quick Access; Legion L becomes View and Legion R becomes Menu."
                    : "Menu opens Steam and View opens Quick Access; Legion L becomes Menu and Legion R becomes View."
                }
                checked={c.swap_menu}
                onChange={(v) => update({ controls: { swap_menu: v } })}
              />
            </PanelSectionRow>
            {c.swap_menu && (
              <PanelSectionRow>
                <DropdownItem
                  label="Layout"
                  rgOptions={LAYOUTS}
                  selectedOption={c.swap_layout}
                  onChange={(o) =>
                    update({ controls: { swap_layout: o.data as "steam_menu" | "steam_view" } })
                  }
                />
              </PanelSectionRow>
            )}
            <ErrorRow message={status.errors.swap_menu} />
          </>
        )}
      </PanelSection>

      <PanelSection title="Back buttons">
        {!caps.remap.available ? (
          <Unavailable cap={caps.remap} what="Button remapping" />
        ) : (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Remap back buttons"
                description="Stored in the controllers themselves. Steam still sees the back buttons too, so leave them unassigned in Steam Input for a game where you use this."
                checked={c.remap_managed}
                onChange={(v) => update({ controls: { remap_managed: v } })}
              />
            </PanelSectionRow>
            {c.remap_managed &&
              BUTTONS.map((b) => (
                <PanelSectionRow key={b.key}>
                  <DropdownItem
                    label={b.label}
                    rgOptions={ACTIONS}
                    selectedOption={c.remap[b.key]}
                    onChange={(o) => update({ controls: { remap: { [b.key]: o.data as string } } })}
                  />
                </PanelSectionRow>
              ))}
            <PanelSectionRow>
              <ToggleField
                label="Touchpad"
                description="The touchpad on the right controller."
                checked={c.touchpad ?? true}
                onChange={(v) => update({ controls: { touchpad: v } })}
              />
            </PanelSectionRow>
            <ErrorRow message={status.errors.remap} />
          </>
        )}
      </PanelSection>
    </>
  );
};
