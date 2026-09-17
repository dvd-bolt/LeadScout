import { useEffect, useState } from "react";
import type { ResumeDraft, ResumeDraftData } from "../../shared/types/api";
import { Button } from "../../shared/ui";
import styles from "../../shared/ui/UI.module.css";

export type ReferenceOption = { id: string; label: string };
type MutateDraft = (mutator: (next: ResumeDraftData) => void) => void;

function TextField({ label, value, onChange, onBlur, type = "text", error }: {
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  onBlur?: () => void;
  type?: string;
  error?: string;
}) {
  return (
    <label className={styles.field}>
      {label}
      <input
        aria-invalid={Boolean(error)}
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
      />
      {error ? <span className={styles.fieldError}>{error}</span> : null}
    </label>
  );
}

function TextArea({ label, value, onChange, onBlur, hint }: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  hint?: string;
}) {
  return (
    <label className={styles.field}>
      {label}
      <textarea value={value} onChange={(event) => onChange(event.target.value)} onBlur={onBlur} />
      {hint ? <span className={styles.meta}>{hint}</span> : null}
    </label>
  );
}

export function SelectField({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}) {
  return (
    <label className={styles.field}>
      {label}
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Выберите значение</option>
        {options.map(([option, text]) => <option value={option} key={option}>{text}</option>)}
      </select>
    </label>
  );
}

function MultiChoiceField({ label, value, options, onChange }: {
  label: string;
  value: string[];
  options: string[];
  onChange: (value: string[]) => void;
}) {
  return (
    <fieldset className={styles.field}>
      <legend>{label}</legend>
      {options.map((option) => (
        <label key={option}>
          <input
            type="checkbox"
            checked={value.includes(option)}
            onChange={(event) => onChange(
              event.target.checked ? [...value, option] : value.filter((item) => item !== option),
            )}
          />
          {option}
        </label>
      ))}
    </fieldset>
  );
}

function ListField({ label, value, separator = ",", onCommit, storageKey }: {
  label: string;
  value: string[];
  separator?: "," | "\n";
  onCommit: (value: string[]) => void;
  storageKey: string;
}) {
  const formatted = value.join(separator === "," ? ", " : "\n");
  const [raw, setRaw] = useState(() => localStorage.getItem(storageKey) ?? formatted);
  useEffect(() => {
    if (localStorage.getItem(storageKey) === null) setRaw(formatted);
  }, [formatted, storageKey]);
  const change = (next: string) => {
    setRaw(next);
    localStorage.setItem(storageKey, next);
  };
  const commit = () => {
    const items = (separator === "," ? raw.split(",") : raw.split("\n"))
      .map((item) => item.trim())
      .filter(Boolean);
    onCommit(items);
    localStorage.removeItem(storageKey);
  };
  return separator === ","
    ? <TextField label={label} value={raw} onChange={change} onBlur={commit} />
    : <TextArea label={label} value={raw} onChange={change} onBlur={commit} />;
}

function SkillListField({ value, onCommit, storageKey }: {
  value: ResumeDraftData["skills"];
  onCommit: (value: ResumeDraftData["skills"]) => void;
  storageKey: string;
}) {
  const formatted = value.map((item) => `${item.name}${item.level ? ` | ${item.level}` : ""}`).join("\n");
  const [raw, setRaw] = useState(() => localStorage.getItem(storageKey) ?? formatted);
  useEffect(() => {
    if (localStorage.getItem(storageKey) === null) setRaw(formatted);
  }, [formatted, storageKey]);
  return (
    <TextArea
      label="Навыки"
      hint="Один навык на строку; уровень укажите через |."
      value={raw}
      onChange={(next) => {
        setRaw(next);
        localStorage.setItem(storageKey, next);
      }}
      onBlur={() => {
        onCommit(raw.split("\n").map((row) => {
          const [name, level = ""] = row.split("|").map((item) => item.trim());
          return { name, level };
        }).filter((item) => item.name));
        localStorage.removeItem(storageKey);
      }}
    />
  );
}

export function ResumeEditorStep({
  step,
  data,
  draft,
  errors,
  backupKey,
  professionOptions,
  cityOptions,
  mutateData,
}: {
  step: string;
  data: ResumeDraftData;
  draft: ResumeDraft;
  errors: Map<string, string>;
  backupKey: string;
  professionOptions: ReferenceOption[];
  cityOptions: ReferenceOption[];
  mutateData: MutateDraft;
}) {
  if (step === "profession") {
    return <>
      <TextField
        label="Название резюме"
        value={data.profession.title}
        error={errors.get("profession.title")}
        onChange={(value) => mutateData((next) => { next.profession.title = value; })}
      />
      {professionOptions.length ? (
        <SelectField
          label="Выберите профессию из вариантов hh.ru"
          value={data.profession.hh_profession_id}
          options={professionOptions.map((value) => [value.id, value.label])}
          onChange={(value) => mutateData((next) => {
            const selected = professionOptions.find((item) => item.id === value);
            next.profession.hh_profession_id = value;
            next.profession.hh_profession = selected?.label ?? "";
          })}
        />
      ) : (
        <TextField
          label="Название профессии для поиска на hh.ru"
          value={data.profession.hh_profession}
          error={errors.get("profession.hh_profession")}
          onChange={(value) => mutateData((next) => {
            next.profession.hh_profession = value;
            next.profession.hh_profession_id = "";
          })}
        />
      )}
      <ListField
        storageKey={`${backupKey}:raw:specializations`}
        label="Специализации, через запятую"
        value={data.profession.specializations}
        onCommit={(value) => mutateData((next) => { next.profession.specializations = value; })}
      />
    </>;
  }

  if (step === "personal") {
    return <>
      <div className={styles.fieldGrid}>
        {(["last_name", "first_name", "middle_name"] as const).map((key) => (
          <TextField
            key={key}
            label={{ last_name: "Фамилия", first_name: "Имя", middle_name: "Отчество" }[key]}
            value={data.personal[key]}
            error={errors.get(`personal.${key}`)}
            onChange={(value) => mutateData((next) => { next.personal[key] = value; })}
          />
        ))}
        <TextField
          label="Дата рождения"
          type="date"
          value={data.personal.birth_date}
          error={errors.get("personal.birth_date")}
          onChange={(value) => mutateData((next) => { next.personal.birth_date = value; })}
        />
        <SelectField
          label="Пол"
          value={data.personal.gender}
          options={[["Мужчина", "Мужской"], ["Женщина", "Женский"]]}
          onChange={(value) => mutateData((next) => { next.personal.gender = value; })}
        />
        {cityOptions.length ? (
          <SelectField
            label="Выберите город из вариантов hh.ru"
            value={data.personal.hh_city_id}
            options={cityOptions.map((value) => [value.id, value.label])}
            onChange={(value) => mutateData((next) => {
              const selected = cityOptions.find((item) => item.id === value);
              next.personal.hh_city_id = value;
              next.personal.city = selected?.label ?? "";
            })}
          />
        ) : (
          <TextField
            label="Город"
            value={data.personal.city}
            error={errors.get("personal.city")}
            onChange={(value) => mutateData((next) => {
              next.personal.city = value;
              next.personal.hh_city_id = "";
            })}
          />
        )}
      </div>
      <ListField
        storageKey={`${backupKey}:raw:citizenships`}
        label="Гражданство, через запятую"
        value={data.personal.citizenships}
        onCommit={(value) => mutateData((next) => { next.personal.citizenships = value; })}
      />
      <ListField
        storageKey={`${backupKey}:raw:work_authorizations`}
        label="Разрешение на работу, через запятую"
        value={data.personal.work_authorizations}
        onCommit={(value) => mutateData((next) => { next.personal.work_authorizations = value; })}
      />
    </>;
  }

  if (step === "contacts") {
    return <div className={styles.fieldGrid}>
      <TextField label="Телефон" value={data.contacts.phone} onChange={(value) => mutateData((next) => { next.contacts.phone = value; })} />
      <TextField label="Email" type="email" value={data.contacts.email} onChange={(value) => mutateData((next) => { next.contacts.email = value; })} />
      <TextField label="Telegram" value={data.contacts.telegram} onChange={(value) => mutateData((next) => { next.contacts.telegram = value; })} />
      <TextField label="Предпочтительный контакт" value={data.contacts.preferred} onChange={(value) => mutateData((next) => { next.contacts.preferred = value; })} />
      <ListField storageKey={`${backupKey}:raw:contact_methods`} label="Способы связи, через запятую" value={data.contacts.methods} onCommit={(value) => mutateData((next) => { next.contacts.methods = value; })} />
    </div>;
  }

  if (step === "conditions") {
    return <div className={styles.fieldGrid}>
      <TextField label="Зарплата" type="number" value={data.work_conditions.salary ?? ""} onChange={(value) => mutateData((next) => { next.work_conditions.salary = value ? Number(value) : null; })} />
      <SelectField label="Валюта" value={data.work_conditions.currency} options={[["RUR", "₽"], ["USD", "$"], ["EUR", "€"]]} onChange={(value) => mutateData((next) => { next.work_conditions.currency = value; })} />
      <MultiChoiceField label="Занятость" value={data.work_conditions.employment_types} options={["Полная занятость", "Частичная занятость", "Проектная работа", "Стажировка"]} onChange={(value) => mutateData((next) => { next.work_conditions.employment_types = value; })} />
      <MultiChoiceField label="График" value={data.work_conditions.schedules} options={["Полный день", "Сменный график", "Гибкий график", "Удалённая работа", "Вахтовый метод"]} onChange={(value) => mutateData((next) => { next.work_conditions.schedules = value; })} />
      <MultiChoiceField label="Формат работы" value={data.work_conditions.work_formats} options={["На месте работодателя", "Удалённо", "Гибрид"]} onChange={(value) => mutateData((next) => { next.work_conditions.work_formats = value; })} />
      <SelectField label="Переезд" value={data.work_conditions.relocation} options={[["Не готов к переезду", "Не готов"], ["Готов к переезду", "Готов"], ["Хочу переехать", "Хочу переехать"]]} onChange={(value) => mutateData((next) => { next.work_conditions.relocation = value; })} />
      <SelectField label="Командировки" value={data.work_conditions.business_trips} options={[["Не готов к командировкам", "Не готов"], ["Готов к редким командировкам", "Редкие"], ["Готов к командировкам", "Готов"]]} onChange={(value) => mutateData((next) => { next.work_conditions.business_trips = value; })} />
    </div>;
  }

  if (step === "skills") {
    return <>
      <SkillListField storageKey={`${backupKey}:skills`} value={data.skills} onCommit={(value) => mutateData((next) => { next.skills = value; })} />
      <div className={styles.meta}>
        Сейчас навыков: {data.skills.length}
        {draft.preflight.capabilities?.max_skills ? ` · лимит hh: ${draft.preflight.capabilities.max_skills}` : ""}
      </div>
    </>;
  }

  if (step === "experience") {
    return <>
      {data.experiences.map((item, index) => (
        <article className={styles.repeatCard} key={`experience-${index}`}>
          <div className={styles.cardHeader}>
            <h3>Место работы {index + 1}</h3>
            <button className={styles.textButton} onClick={() => mutateData((next) => { next.experiences.splice(index, 1); })}>Удалить</button>
          </div>
          <label className={styles.switch}>
            <input type="checkbox" checked={item.selected} onChange={(event) => mutateData((next) => { next.experiences[index].selected = event.target.checked; })} />
            Переносить в резюме
          </label>
          <div className={styles.fieldGrid}>
            {(["company", "position", "city", "start_month", "start_year", "end_month", "end_year"] as const).map((key) => (
              <TextField
                key={key}
                label={{ company: "Компания", position: "Должность", city: "Город", start_month: "Месяц начала", start_year: "Год начала", end_month: "Месяц окончания", end_year: "Год окончания" }[key]}
                value={item[key]}
                error={errors.get(`experiences.${index}.${key}`)}
                onChange={(value) => mutateData((next) => { next.experiences[index][key] = value; })}
              />
            ))}
          </div>
          <label className={styles.switch}>
            <input type="checkbox" checked={item.is_current} onChange={(event) => mutateData((next) => { next.experiences[index].is_current = event.target.checked; })} />
            Работаю сейчас
          </label>
          <TextArea label="Обязанности и достижения" value={item.description} onChange={(value) => mutateData((next) => { next.experiences[index].description = value; })} />
        </article>
      ))}
      <Button className={styles.secondary} onClick={() => mutateData((next) => { next.experiences.push({ company: "", position: "", city: "", start_month: "", start_year: "", is_current: false, end_month: "", end_year: "", description: "", selected: true }); })}>Добавить место работы</Button>
    </>;
  }

  if (step === "education") {
    return <>
      {data.education.map((item, index) => (
        <article className={styles.repeatCard} key={`education-${index}`}>
          <div className={styles.cardHeader}>
            <h3>Образование {index + 1}</h3>
            <button className={styles.textButton} onClick={() => mutateData((next) => { next.education.splice(index, 1); })}>Удалить</button>
          </div>
          <label className={styles.switch}>
            <input type="checkbox" checked={item.selected} onChange={(event) => mutateData((next) => { next.education[index].selected = event.target.checked; })} />
            Переносить в резюме
          </label>
          <div className={styles.fieldGrid}>
            <SelectField label="Уровень" value={item.level} options={["Среднее", "Среднее специальное", "Неоконченное высшее", "Высшее", "Бакалавр", "Магистр", "Кандидат наук", "Доктор наук"].map((value) => [value, value])} onChange={(value) => mutateData((next) => { next.education[index].level = value; })} />
            {(["institution", "faculty", "specialization", "end_year"] as const).map((key) => (
              <TextField
                key={key}
                label={{ institution: "Учебное заведение", faculty: "Факультет", specialization: "Специальность", end_year: "Год окончания" }[key]}
                value={item[key]}
                error={errors.get(`education.${index}.${key}`)}
                onChange={(value) => mutateData((next) => { next.education[index][key] = value; })}
              />
            ))}
          </div>
        </article>
      ))}
      <Button className={styles.secondary} onClick={() => mutateData((next) => { next.education.push({ level: "", institution: "", faculty: "", specialization: "", end_year: "", selected: true }); })}>Добавить образование</Button>
    </>;
  }

  if (step === "languages") {
    return <>
      {data.languages.map((item, index) => (
        <div className={styles.inlineFields} key={`language-${index}`}>
          <TextField label="Язык" value={item.name} onChange={(value) => mutateData((next) => { next.languages[index].name = value; })} />
          <SelectField label="Уровень" value={item.level} options={["A1", "A2", "B1", "B2", "C1", "C2", "Родной"].map((value) => [value, value])} onChange={(value) => mutateData((next) => { next.languages[index].level = value; })} />
          <button className={styles.textButton} onClick={() => mutateData((next) => { next.languages.splice(index, 1); })}>Удалить</button>
        </div>
      ))}
      <Button className={styles.secondary} onClick={() => mutateData((next) => { next.languages.push({ name: "", level: "" }); })}>Добавить язык</Button>
    </>;
  }

  if (step === "additional") {
    const labels = { courses: "Курсы", exams: "Экзамены", certificates: "Сертификаты", recommendations: "Рекомендации" };
    return <>
      {(["courses", "exams", "certificates", "recommendations"] as const).map((key) => (
        <section key={key}>
          <h3>{labels[key]}</h3>
          {data.additional[key].map((item, index) => (
            <article className={styles.repeatCard} key={`${key}-${index}`}>
              <div className={styles.cardHeader}>
                <strong>Запись {index + 1}</strong>
                <button className={styles.textButton} onClick={() => mutateData((next) => { next.additional[key].splice(index, 1); })}>Удалить</button>
              </div>
              <div className={styles.fieldGrid}>
                <TextField label="Название" value={item.name} onChange={(value) => mutateData((next) => { next.additional[key][index].name = value; })} />
                <TextField label="Организация" value={item.organization} onChange={(value) => mutateData((next) => { next.additional[key][index].organization = value; })} />
                <TextField label="Год" value={item.year} onChange={(value) => mutateData((next) => { next.additional[key][index].year = value; })} />
              </div>
              <TextArea label="Описание" value={item.description} onChange={(value) => mutateData((next) => { next.additional[key][index].description = value; })} />
            </article>
          ))}
          <Button className={styles.secondary} onClick={() => mutateData((next) => { next.additional[key].push({ name: "", organization: "", year: "", description: "" }); })}>Добавить запись</Button>
        </section>
      ))}
      <ListField storageKey={`${backupKey}:raw:driving_licenses`} label="Категории водительских прав, через запятую" value={data.additional.driving_licenses} onCommit={(value) => mutateData((next) => { next.additional.driving_licenses = value; })} />
      <label className={styles.switch}>
        <input type="checkbox" checked={data.additional.has_car} onChange={(event) => mutateData((next) => { next.additional.has_car = event.target.checked; })} />
        Есть автомобиль
      </label>
    </>;
  }

  if (step === "about") {
    return <>
      <TextArea label="О себе" value={data.about.text} onChange={(value) => mutateData((next) => { next.about.text = value; })} />
      {data.about.links.map((item, index) => (
        <div className={styles.inlineFields} key={`link-${index}`}>
          <TextField label="Название ссылки" value={item.label} onChange={(value) => mutateData((next) => { next.about.links[index].label = value; })} />
          <TextField label="URL" type="url" value={item.url} onChange={(value) => mutateData((next) => { next.about.links[index].url = value; })} />
          <button className={styles.textButton} onClick={() => mutateData((next) => { next.about.links.splice(index, 1); })}>Удалить</button>
        </div>
      ))}
      <Button className={styles.secondary} onClick={() => mutateData((next) => { next.about.links.push({ label: "", url: "" }); })}>Добавить ссылку</Button>
    </>;
  }

  return null;
}
