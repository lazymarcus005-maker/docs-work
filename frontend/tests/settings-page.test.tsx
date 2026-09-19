import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPage from "@/app/settings/page";
import { api } from "@/lib/api";

const apiMock = vi.hoisted(() => ({
  profiles: vi.fn(),
  jevSettings: vi.fn(),
  processingSettings: vi.fn(),
  limits: vi.fn(),
  saveJevSettings: vi.fn(),
  testJev: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));

const disabledSettings = {
  enabled: false,
  has_api_key: false,
  ready: false,
  model: "jev-1.13.0",
};

const configuredSettings = {
  ...disabledSettings,
  has_api_key: true,
};

const enabledSettings = {
  ...configuredSettings,
  enabled: true,
  ready: true,
};

const mockedApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.profiles.mockResolvedValue({ profiles: [] });
  apiMock.jevSettings.mockResolvedValue(disabledSettings);
  apiMock.processingSettings.mockResolvedValue({});
  apiMock.limits.mockResolvedValue({
    tokens_used_today: 0,
    daily_token_budget: 0,
    kill_switch: false,
  });
  apiMock.saveJevSettings.mockResolvedValue(configuredSettings);
  apiMock.testJev.mockResolvedValue({
    reachable: true,
    model: "jev-1.13.0",
    latency_ms: 42,
  });
});

afterEach(() => cleanup());

describe("Jev Settings", () => {
  it("keeps routing disabled until an API key is configured", async () => {
    render(<SettingsPage />);

    const toggle = await screen.findByRole("switch", { name: "Enable Jev for chat routing" });

    expect(toggle.getAttribute("aria-disabled")).toBe("true");
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("saves the API key without displaying it again", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    const keyInput = await screen.findByLabelText(/TypeSafe API key/);
    await user.type(keyInput, "ts-test-secret");
    await user.click(screen.getByRole("button", { name: "Save key" }));

    await waitFor(() => expect(mockedApi.saveJevSettings).toHaveBeenCalledWith({ api_key: "ts-test-secret" }));
    expect((keyInput as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("switch", { name: "Enable Jev for chat routing" }).getAttribute("aria-checked")).toBe("false");
  });

  it("tests connectivity without enabling Jev", async () => {
    const user = userEvent.setup();
    apiMock.jevSettings.mockResolvedValue(configuredSettings);
    render(<SettingsPage />);

    await user.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText(/Reachable · jev-1.13.0 · 42 ms/)).toBeTruthy();
    expect(mockedApi.testJev).toHaveBeenCalledOnce();
    expect(mockedApi.saveJevSettings).not.toHaveBeenCalled();
    expect(screen.getByRole("switch", { name: "Enable Jev for chat routing" }).getAttribute("aria-checked")).toBe("false");
  });

  it("sends the enable choice after the saved key is available", async () => {
    const user = userEvent.setup();
    apiMock.jevSettings.mockResolvedValue(configuredSettings);
    apiMock.saveJevSettings.mockResolvedValue(enabledSettings);
    render(<SettingsPage />);

    await user.click(await screen.findByRole("switch", { name: "Enable Jev for chat routing" }));

    await waitFor(() => expect(mockedApi.saveJevSettings).toHaveBeenCalledWith({ enabled: true }));
    expect(screen.getByRole("switch", { name: "Enable Jev for chat routing" }).getAttribute("aria-checked")).toBe("true");
  });
});
