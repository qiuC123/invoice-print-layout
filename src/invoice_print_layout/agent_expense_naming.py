"""Short automatic names are a projection; source goods and manual names stay intact."""
from __future__ import annotations

from collections.abc import Sequence
import re


# These modifiers describe rainwear, not another purchased article. Unknown
# accessories, brands or model strings stay verbatim rather than being guessed.
_RAINWEAR_MODIFIER = (
    r'(?:便携式|便携|携式|小巧|一次性|成人|儿童|男女|通用|加厚|防水|透明|'
    r'雨季|出行|长款|短款|长|蓝色|黄色|白色|黑色|红色|绿色|'
    r'\d+(?:个|件|套|只|包|码|号)|[【】\[\]（）()])'
)
_RAINWEAR = re.compile(r'^' + _RAINWEAR_MODIFIER + r'*(?:雨衣|雨披)' + _RAINWEAR_MODIFIER + r'*$')


def concise_expense_name(names: Sequence[str], category: str) -> str:
    """Keep concrete nouns whole; detailed descriptions remain in the source list.

    Only an explicit, fully covered alias may collapse two descriptions. Never
    slice a product or model in half merely to satisfy a character count.
    """
    labels: list[str] = []
    for name in names:
        label = name.strip()
        compact = re.sub(r'\s+', '', label)
        if _RAINWEAR.fullmatch(compact):
            label = '雨衣'
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return ''
    selected = labels[:3]

    def heading() -> str:
        result = '、'.join(selected) + ('等' if len(selected) < len(labels) else '')
        if category == '材料采购' and not result.endswith('采购'):
            result += '采购'
        return result

    # Omit whole later labels instead of cutting a brand/model mid-word. A
    # single concrete name may exceed this soft target and remains complete.
    while len(selected) > 1 and len(heading()) > 80:
        selected.pop()
    title = heading()
    if len(title) > 200:
        raise ValueError('商品名称过长，请在人工工作台填写简洁报销名称；完整商品信息保留')
    return title
