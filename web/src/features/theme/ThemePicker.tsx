import { Card } from "../../shared/ui";
import { THEME_OPTIONS, useTheme } from "./ThemeProvider";
import styles from "./ThemePicker.module.css";

export function ThemePicker() {
  const { theme, setTheme } = useTheme();

  return <Card className={styles.card}>
    <fieldset className={styles.fieldset}>
      <legend>Тема оформления</legend>
      <p className={styles.hint}>Применяется сразу и сохраняется на этом устройстве.</p>
      <div className={styles.options}>
        {THEME_OPTIONS.map((option) => <label className={styles.option} key={option.name}>
          <input
            className={styles.radio}
            type="radio"
            name="theme"
            value={option.name}
            aria-label={`${option.label} тема`}
            checked={theme === option.name}
            onChange={() => setTheme(option.name)}
          />
          <span className={`${styles.tile} ${styles[option.name]}`}>
            <span className={styles.preview} aria-hidden="true"><i /><i /><i /></span>
            <span className={styles.copy}>
              <strong>{option.label}</strong>
              <small>{option.description}</small>
            </span>
            <span className={styles.check} aria-hidden="true">✓</span>
          </span>
        </label>)}
      </div>
    </fieldset>
  </Card>;
}
