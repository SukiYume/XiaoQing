/** 图表的分类配色。取值是 css/app.css 的色相令牌，冷暖交替排列，相邻分类容易区分。 */

const CATEGORY_HUES = Object.freeze(['red', 'blue', 'gold', 'violet', 'mint', 'pink', 'amber']);

/** 返回从指定色相起排的分类色，图表的第一类沿用所在模块的色标。 */
export function categoryColors(lead) {
    const start = Math.max(0, CATEGORY_HUES.indexOf(lead));
    return [...CATEGORY_HUES.slice(start), ...CATEGORY_HUES.slice(0, start)].map((hue) => `var(--${hue})`);
}

/** 按强度（0 到 1）混合出热力格的底色。 */
export function heatColor(color, strength) {
    return `color-mix(in srgb, ${color} ${Math.round(strength * 100)}%, transparent)`;
}
