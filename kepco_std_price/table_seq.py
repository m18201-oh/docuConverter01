"""구획 번호 table_seq — 한 ■ 그룹 안 표·주석 묶음의 순번.

변환 한 번(extract_pdf / extract_from_root)에 TableSeq 하나를 만든다.
상태는 group_id 별로만 두고, 그룹 dict 에는 넣지 않는다.
"""


class TableSeq:
    def __init__(self) -> None:
        # group_id -> [번호, 주석 봄, 레코드 있음]
        self._st: dict = {}

    def start(self, group_id):
        if group_id is None:
            return None
        self._st[group_id] = [1, False, False]
        return None

    def note_seen(self, group_id):
        if group_id is None:
            return None
        st = self._st.setdefault(group_id, [1, False, False])
        st[1] = True
        return st[0]

    def record(self, group_id):
        if group_id is None:
            return None
        st = self._st.setdefault(group_id, [1, False, False])
        if st[1] and st[2]:
            st[0] += 1
        st[1] = False
        st[2] = True
        return st[0]
