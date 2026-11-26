import { Action, ActionPanel, Alert, Detail, Form, Icon, LocalStorage, Toast, confirmAlert, popToRoot, showToast, trash } from "@raycast/api";
import fs from "node:fs";
import { useEffect, useState } from "react";
import {
  ABSTRACT_FIELDS,
  API_KEY_KEY,
  CLEAR_API_KEY_PAIR,
  CROP_KEY,
  Config,
  DEFAULT_MODE_KEY,
  FONT_FIELDS,
  FormValues,
  MODES,
  NOT_INSTALLED,
  OTHER_MODEL,
  apiKeyPlaceholder,
  apiKeySet,
  configChanges,
  configErrors,
  configFile,
  customModelKey,
  effortChoices,
  effortKey,
  formValues,
  modelChoices,
  modelKey,
  parseRecentDays,
  timeoutKey,
} from "./core";
import { TONGTU_HOME, USER_HOME, installed, loadRecentDays, readConfig, tongtu } from "./tongtu";

const RECENT_DAYS_FIELD = "recentDays";

type Loaded = { config: Config; recentDays: number };

export default function Settings() {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failure, setFailure] = useState("");
  const [values, setValues] = useState<FormValues>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function load(): Promise<void> {
    if (!installed()) {
      setFailure(NOT_INSTALLED);
      return;
    }
    const config = await readConfig();
    if (!config) {
      setFailure("tongtu config list --json 没有给出结果");
      return;
    }
    const recentDays = await loadRecentDays();
    setLoaded({ config, recentDays });
    setValues({ ...formValues(config), [RECENT_DAYS_FIELD]: String(recentDays) });
    setErrors({});
  }

  useEffect(() => {
    load();
  }, []);

  if (failure) return <Detail markdown={failure} />;
  if (!loaded) return <Form isLoading />;

  const text = (key: string) => String(values[key] ?? "");
  const change = (key: string) => (value: string | boolean) => {
    setValues((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: "" }));
  };
  const error = (key: string) => errors[key] || undefined;

  async function save(): Promise<void> {
    if (!loaded) return;
    const { pairs, errors: invalid } = configChanges(loaded.config, values);
    const found = { ...invalid };
    const days = parseRecentDays(text(RECENT_DAYS_FIELD));
    const daysChanged = days !== null && days !== loaded.recentDays;
    if (days === null) found[RECENT_DAYS_FIELD] = "要求是正整数";
    if (daysChanged) await LocalStorage.setItem(RECENT_DAYS_FIELD, String(days));
    let config = loaded.config;
    let rejected = "";
    if (Object.keys(invalid).length === 0 && pairs.length > 0) {
      const result = await tongtu(["config", "set", "--", ...pairs]);
      if (result.code === 0) {
        config = (await readConfig()) ?? config;
      } else {
        const reported = configErrors(result.stderr, Object.keys(values));
        Object.assign(found, reported.fields);
        rejected = reported.first || "tongtu config set 失败";
      }
    }
    setLoaded({ config, recentDays: daysChanged && days !== null ? days : loaded.recentDays });
    if (!rejected && pairs.includes(API_KEY_KEY)) setValues((current) => ({ ...current, [API_KEY_KEY]: "" }));
    setErrors(found);
    if (rejected) {
      await showToast({ style: Toast.Style.Failure, title: rejected });
    } else if (Object.keys(found).length > 0) {
      await showToast({ style: Toast.Style.Failure, title: Object.values(found)[0] });
    } else if (pairs.length === 0 && !daysChanged) {
      await showToast({ style: Toast.Style.Success, title: "没有改动" });
    } else {
      await showToast({ style: Toast.Style.Success, title: "已保存" });
    }
  }

  async function clearApiKey(): Promise<void> {
    if (!loaded) return;
    const result = await tongtu(["config", "set", "--", ...CLEAR_API_KEY_PAIR]);
    if (result.code !== 0) {
      await showToast({ style: Toast.Style.Failure, title: result.stderr.trim() || "tongtu config set 失败" });
      return;
    }
    const config = (await readConfig()) ?? loaded.config;
    setLoaded({ ...loaded, config });
    setValues((current) => ({ ...current, [API_KEY_KEY]: "" }));
    await showToast({ style: Toast.Style.Success, title: "已清除摘要翻译的密钥" });
  }

  async function openConfig(): Promise<void> {
    const result = await tongtu(["config", "open"]);
    if (result.code !== 0) await showToast({ style: Toast.Style.Failure, title: result.stderr.trim() || "tongtu config open 失败" });
  }

  async function reset(): Promise<void> {
    const confirmed = await confirmAlert({
      title: "恢复默认值？",
      message: "config.toml 会移到废纸篓，全部配置项回到默认值。",
      primaryAction: { title: "恢复默认值", style: Alert.ActionStyle.Destructive },
    });
    if (!confirmed) return;
    const file = configFile(USER_HOME, TONGTU_HOME);
    if (fs.existsSync(file)) await trash(file);
    await load();
    await showToast({ style: Toast.Style.Success, title: "已恢复默认值" });
  }

  return (
    <Form
      actions={
        <ActionPanel>
          <Action.SubmitForm title="保存" icon={Icon.Check} onSubmit={save} />
          <Action title="关闭" icon={Icon.XMarkCircle} onAction={() => popToRoot()} />
          {apiKeySet(loaded.config) && <Action title="清除摘要翻译的密钥" icon={Icon.Key} style={Action.Style.Destructive} onAction={clearApiKey} />}
          <Action title="打开 config.toml" icon={Icon.Document} onAction={openConfig} />
          <Action title="恢复默认值" icon={Icon.Trash} style={Action.Style.Destructive} onAction={reset} />
        </ActionPanel>
      }
    >
      <Form.Dropdown id={DEFAULT_MODE_KEY} title="默认模式" value={text(DEFAULT_MODE_KEY)} onChange={change(DEFAULT_MODE_KEY)} error={error(DEFAULT_MODE_KEY)}>
        {MODES.map((mode) => (
          <Form.Dropdown.Item key={mode} title={mode} value={mode} />
        ))}
      </Form.Dropdown>
      {MODES.map((mode) => {
        const currentModel = String(loaded.config[modelKey(mode)] ?? "");
        const currentEffort = String(loaded.config[effortKey(mode)] ?? "");
        return [
          <Form.Separator key={`${mode}.separator`} />,
          <Form.Description key={`${mode}.title`} text={`${mode} 模式`} />,
          <Form.Dropdown key={modelKey(mode)} id={modelKey(mode)} title="模型" value={text(modelKey(mode))} onChange={change(modelKey(mode))} error={error(modelKey(mode))}>
            {modelChoices(currentModel).map((model) => (
              <Form.Dropdown.Item key={model} title={model} value={model} />
            ))}
            <Form.Dropdown.Item title="其他…" value={OTHER_MODEL} />
          </Form.Dropdown>,
          text(modelKey(mode)) === OTHER_MODEL && (
            <Form.TextField
              key={customModelKey(mode)}
              id={customModelKey(mode)}
              title="其他模型"
              placeholder="运行时/模型"
              value={text(customModelKey(mode))}
              onChange={change(customModelKey(mode))}
            />
          ),
          <Form.Dropdown key={effortKey(mode)} id={effortKey(mode)} title="推理强度" value={text(effortKey(mode))} onChange={change(effortKey(mode))} error={error(effortKey(mode))}>
            {effortChoices(currentEffort).map((effort) => (
              <Form.Dropdown.Item key={effort} title={effort || "未设置"} value={effort} />
            ))}
          </Form.Dropdown>,
          <Form.TextField
            key={timeoutKey(mode)}
            id={timeoutKey(mode)}
            title="会话时限（分钟）"
            value={text(timeoutKey(mode))}
            onChange={change(timeoutKey(mode))}
            error={error(timeoutKey(mode))}
          />,
        ];
      })}
      <Form.Separator />
      <Form.Description text="PDF" />
      <Form.Checkbox id={CROP_KEY} label="裁掉页面白边" value={values[CROP_KEY] === true} onChange={change(CROP_KEY)} error={error(CROP_KEY)} />
      <Form.Separator />
      <Form.Description text="字体" />
      {FONT_FIELDS.map(([key, title]) => (
        <Form.TextField key={key} id={key} title={title} info="文件名或绝对路径" placeholder="文件名或绝对路径" value={text(key)} onChange={change(key)} error={error(key)} />
      ))}
      <Form.Separator />
      <Form.Description text="摘要翻译" />
      {ABSTRACT_FIELDS.map(([key, title]) => (
        <Form.TextField key={key} id={key} title={title} value={text(key)} onChange={change(key)} error={error(key)} />
      ))}
      <Form.TextField
        id={API_KEY_KEY}
        title="密钥"
        placeholder={apiKeyPlaceholder(loaded.config)}
        value={text(API_KEY_KEY)}
        onChange={change(API_KEY_KEY)}
        error={error(API_KEY_KEY)}
      />
      <Form.Separator />
      <Form.Description text="插件" />
      <Form.TextField
        id={RECENT_DAYS_FIELD}
        title="「最近」分区的天数"
        value={text(RECENT_DAYS_FIELD)}
        onChange={change(RECENT_DAYS_FIELD)}
        error={error(RECENT_DAYS_FIELD)}
      />
    </Form>
  );
}
