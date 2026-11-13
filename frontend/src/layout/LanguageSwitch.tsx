import { useTranslation } from 'react-i18next';
import { SUPPORTED_LANGUAGES, setLanguage, type Language } from '../i18n';

export function LanguageSwitch() {
  const { t, i18n } = useTranslation();
  return (
    <label className="language-switch">
      <span>{t('language.label')}</span>
      <select value={i18n.language} onChange={(event) => void setLanguage(event.target.value as Language)}>
        {SUPPORTED_LANGUAGES.map((language) => (
          <option key={language} value={language}>
            {t(`language.${language}`)}
          </option>
        ))}
      </select>
    </label>
  );
}
