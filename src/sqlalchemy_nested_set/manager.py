import logging
from sqlalchemy import event, select, func, inspect
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class NestedSetError(Exception):
    pass


class NestedSetManager:
    def __init__(self):
        self._registry: dict[type, str] = {}
        self._flush_registered = False

    def register(self, model, parent_column: str = "parent_id"):
        if model in self._registry:
            return

        self._registry[model] = parent_column

        if not self._flush_registered:
            event.listen(Session, "before_flush", self._handle_before_flush)
            self._flush_registered = True

    def _parent_col(self, model: type) -> str:
        try:
            return self._registry[model]
        except KeyError:
            raise NestedSetError(
                f"Model {model.__name__} is not registered. "
                "Call NestedSetManager.register() first."
            )

    def _move_subtree(
        self,
        connection,
        table,
        left_attr,
        right_attr,
        model,
        obj,
        l1: int,
        r1: int,
        width: int,
        new_parent_id,
        parent_col: str,
    ):
        id_attr = model.id

        subtree_ids = [
            row[0] for row in connection.execute(
                select(id_attr).where(
                    left_attr >= l1, right_attr <= r1
                )
            ).all()
        ]

        not_subtree = ~id_attr.in_(subtree_ids)

        connection.execute(
            table.update()
            .where(left_attr > r1, not_subtree)
            .values(left=left_attr - width)
        )
        connection.execute(
            table.update()
            .where(right_attr > r1, not_subtree)
            .values(right=right_attr - width)
        )

        if new_parent_id is not None:
            parent_row = connection.execute(
                select(left_attr, right_attr)
                .where(model.id == new_parent_id)
                .with_for_update()
            ).first()
            if parent_row is None:
                return
            target_left = parent_row.right
        else:
            max_right = (
                connection.execute(
                    select(func.max(right_attr)).select_from(table)
                ).scalar()
                or 0
            )
            target_left = max_right + 1

        connection.execute(
            table.update()
            .where(left_attr >= target_left, not_subtree)
            .values(left=left_attr + width)
        )
        connection.execute(
            table.update()
            .where(right_attr >= target_left, not_subtree)
            .values(right=right_attr + width)
        )

        delta = target_left - l1
        connection.execute(
            table.update()
            .where(id_attr.in_(subtree_ids))
            .values({
                left_attr: left_attr + delta,
                right_attr: right_attr + delta,
            })
        )

        parent_col_attr = getattr(model, parent_col)
        connection.execute(
            table.update()
            .where(id_attr == obj.id)
            .values({parent_col_attr: new_parent_id})
        )

        obj.left = target_left
        obj.right = target_left + width - 1

    def _handle_before_flush(self, session, flush_context, instances):
        for model, parent_col in list(self._registry.items()):
            self._process_deletes(session, model)
            self._process_inserts(session, model, parent_col)
            self._process_moves(session, model, parent_col)

    def _process_deletes(self, session: Session, model: type):
        table = model.__table__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        for obj in sorted(
            list(session.deleted),
            key=lambda o: getattr(o, "left", 0),
        ):
            if not isinstance(obj, model):
                continue

            current = session.execute(
                select(left_attr, right_attr)
                .where(model.id == obj.id)
                .with_for_update()
            ).first()

            if current is None:
                continue

            l1, r1 = current.left, current.right
            connection = session.connection()
            parent_col_attr = getattr(model, self._parent_col(model))
            obj_parent_id = getattr(obj, self._parent_col(model))

            if r1 - l1 > 1:
                connection.execute(
                    table.update()
                    .where(getattr(model, self._parent_col(model)) == obj.id)
                    .values({parent_col_attr: obj_parent_id})
                )

                connection.execute(
                    table.update()
                    .where(left_attr > l1, right_attr < r1)
                    .values({
                        left_attr: left_attr - 1,
                        right_attr: right_attr - 1,
                    })
                )

            connection.execute(
                table.update()
                .where(left_attr > r1)
                .values(left=left_attr - 2)
            )
            connection.execute(
                table.update()
                .where(right_attr > r1)
                .values(right=right_attr - 2)
            )

    def _process_inserts(
        self, session: Session, model: type, parent_col: str
    ):
        table = model.__table__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        for obj in list(session.new):
            if not isinstance(obj, model):
                continue

            parent_id = getattr(obj, parent_col)
            connection = session.connection()

            if parent_id is not None:
                parent_row = connection.execute(
                    select(left_attr, right_attr)
                    .where(model.id == parent_id)
                    .with_for_update()
                ).first()

                if parent_row is None:
                    max_right = (
                        connection.execute(
                            select(func.max(right_attr)).select_from(table)
                        ).scalar()
                        or 0
                    )
                    obj.left = max_right + 1
                    obj.right = max_right + 2
                    continue

                parent_right = parent_row.right

                connection.execute(
                    table.update()
                    .where(left_attr >= parent_right)
                    .values(left=left_attr + 2)
                )
                connection.execute(
                    table.update()
                    .where(right_attr >= parent_right)
                    .values(right=right_attr + 2)
                )

                parent_obj = session.get(model, parent_id)
                if parent_obj is not None:
                    parent_obj.right = parent_right + 2

                obj.left = parent_right
                obj.right = parent_right + 1
            else:
                max_right = (
                    connection.execute(
                        select(func.max(right_attr)).select_from(table)
                    ).scalar()
                    or 0
                )
                obj.left = max_right + 1
                obj.right = max_right + 2

    def _process_moves(
        self, session: Session, model: type, parent_col: str
    ):
        table = model.__table__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        for obj in list(session.dirty):
            if not isinstance(obj, model):
                continue

            history = inspect(obj).attrs[parent_col].history
            if not history.has_changes():
                continue

            new_parent_id = getattr(obj, parent_col)

            current = session.execute(
                select(left_attr, right_attr)
                .where(model.id == obj.id)
                .with_for_update()
            ).first()

            if current is None:
                continue

            l1, r1 = current.left, current.right
            width = r1 - l1 + 1
            connection = session.connection()

            self._move_subtree(
                connection=connection,
                table=table,
                left_attr=left_attr,
                right_attr=right_attr,
                model=model,
                obj=obj,
                l1=l1,
                r1=r1,
                width=width,
                new_parent_id=new_parent_id,
                parent_col=parent_col,
            )

            setattr(obj, parent_col, new_parent_id)

    def move(self, session: Session, node, new_parent_id):
        model = node.__class__
        parent_col = self._parent_col(model)
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        if new_parent_id is not None:
            new_parent = session.get(model, new_parent_id)
            if new_parent is None:
                raise NestedSetError(
                    f"Parent with id={new_parent_id} not found"
                )
            if new_parent.id == node.id:
                raise NestedSetError("Cannot move a node to itself")

        result = session.execute(
            select(left_attr, right_attr).where(model.id == node.id)
        ).first()

        if result is None:
            raise NestedSetError(f"Node with id={node.id} not found in DB")

        l1, r1 = result.left, result.right
        width = r1 - l1 + 1

        connection = session.connection()

        self._move_subtree(
            connection=connection,
            table=model.__table__,
            left_attr=left_attr,
            right_attr=right_attr,
            model=model,
            obj=node,
            l1=l1,
            r1=r1,
            width=width,
            new_parent_id=new_parent_id,
            parent_col=parent_col,
        )

        setattr(node, parent_col, new_parent_id)

    def delete(
        self, session: Session, node, include_descendants: bool = True
    ):
        model = node.__class__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        current = session.execute(
            select(left_attr, right_attr)
            .where(model.id == node.id)
            .with_for_update()
        ).first()

        if current is None:
            return

        l1, r1 = current.left, current.right

        if include_descendants and r1 - l1 > 1:
            session.query(model).filter(
                left_attr > l1,
                right_attr < r1,
            ).delete(synchronize_session=False)

        session.delete(node)
        session.flush()

    def rebuild(self, session: Session, model: type):
        parent_col = self._parent_col(model)
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        nodes = session.query(model).order_by(model.id).all()

        children: dict = {}
        for node in nodes:
            pid = getattr(node, parent_col)
            if pid is not None:
                children.setdefault(pid, []).append(node)

        root_nodes = [
            n for n in nodes if getattr(n, parent_col) is None
        ]

        def assign(parent, counter: int) -> int:
            left = counter
            right = counter + 1

            pid = parent.id if parent else None
            for child in children.get(pid, []):
                right = assign(child, right)

            if parent is not None:
                parent.left = left
                parent.right = right

            return right + 1

        counter = 1
        for root in root_nodes:
            counter = assign(root, counter)

        session.flush()

    def ancestors(self, session: Session, node):
        model = node.__class__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")
        return (
            session.query(model)
            .filter(left_attr < node.left, right_attr > node.right)
            .order_by(left_attr)
        )

    def descendants(
        self, session: Session, node, include_self: bool = False
    ):
        model = node.__class__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")

        if include_self:
            q = session.query(model).filter(
                left_attr >= node.left,
                right_attr <= node.right,
            )
        else:
            q = session.query(model).filter(
                left_attr > node.left,
                right_attr < node.right,
            )

        return q.order_by(left_attr)

    def children(self, session: Session, node):
        model = node.__class__
        parent_col = self._parent_col(model)
        return session.query(model).filter(
            getattr(model, parent_col) == node.id
        )

    def subtree(self, session: Session, node):
        model = node.__class__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")
        return (
            session.query(model)
            .filter(left_attr >= node.left, right_attr <= node.right)
            .order_by(left_attr)
        )

    def siblings(self, session: Session, node):
        model = node.__class__
        parent_col = self._parent_col(model)
        pid = getattr(node, parent_col)
        return session.query(model).filter(
            getattr(model, parent_col) == pid,
            model.id != node.id,
        )

    def depth(self, session: Session, node):
        model = node.__class__
        left_attr = getattr(model, "left")
        right_attr = getattr(model, "right")
        return (
            session.query(func.count(model.id))
            .filter(left_attr < node.left, right_attr > node.right)
            .scalar()
            or 0
        )

    def get_roots(self, session: Session, model: type):
        parent_col = self._parent_col(model)
        left_attr = getattr(model, "left")
        return (
            session.query(model)
            .filter(getattr(model, parent_col) == None)
            .order_by(left_attr)
        )

    def get_tree(self, session: Session, model: type):
        left_attr = getattr(model, "left")
        return session.query(model).order_by(left_attr).all()

    def is_leaf(self, node) -> bool:
        return node.right == node.left + 1

    def is_root(self, node) -> bool:
        parent_col = self._parent_col(node.__class__)
        return getattr(node, parent_col) is None
