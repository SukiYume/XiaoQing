/** 像素图标的 HTML 片段。图形定义在 css/icons.css，颜色跟随所在文字。 */

const MOOD_FACES = new Set(['happy', 'sad', 'calm', 'excited', 'angry', 'tired', 'anxious', 'grateful', 'neutral']);
const WEATHER_ICONS = Object.freeze([
    ['多云', 'cloud'],
    ['晴', 'sun'],
    ['雨', 'rain'],
    ['雪', 'snow'],
    ['雾', 'fog'],
    ['风', 'wind'],
    ['云', 'cloud'],
    ['阴', 'cloud'],
]);

export function pixelIcon(name, className = '') {
    return `<span class="pixel-icon${className ? ` ${className}` : ''}" data-icon="${name}" aria-hidden="true"></span>`;
}

/** 心情编号对应的像素表情，编号之外的心情用通用表情。 */
export function moodIcon(mood, className = '') {
    const id = String(mood || '')
        .trim()
        .toLowerCase();
    return pixelIcon(MOOD_FACES.has(id) ? `mood-${id}` : 'mood-other', className);
}

/** 认得出的天气配一个像素图标，认不出时返回空串。 */
export function weatherIcon(value, className = '') {
    const text  = String(value || '');
    const match = WEATHER_ICONS.find(([word]) => text.includes(word));
    return match ? pixelIcon(match[1], className) : '';
}
