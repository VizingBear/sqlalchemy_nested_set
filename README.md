# sqlalchemy_nested_set

Nested Set (MPTT) implementation for SQLAlchemy 2.0+.

Автоматически управляет колонками `left` и `right` для древовидных структур. Колонка `parent_id` определяется пользователем в модели.

## Установка

```bash
pip install sqlalchemy-nested-set
```

## Использование

### 1. Определите модель с миксином

```python
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy_nested_set import NestedSetMixin, NestedSetManager

class Base(DeclarativeBase):
    pass

class Category(NestedSetMixin, Base):
    __tablename__ = 'categories'
    
    id = Column(Integer, primary_key=True)
    name = Column(String)
    parent_id = Column(Integer, ForeignKey('categories.id'))
```

### 2. Зарегистрируйте модель в менеджере

```python
ns = NestedSetManager()
ns.register(Category, parent_column='parent_id')
```

После регистрации `left` и `right` заполняются автоматически при добавлении новых записей.

### 3. Добавление записей

```python
root = Category(name='root')
session.add(root)
session.flush()  # left=1, right=2

child = Category(name='child', parent_id=root.id)
session.add(child)
session.flush()  # root: left=1, right=4; child: left=2, right=3
```

### 4. Перемещение узла

```python
ns.move(session, node, new_parent_id=123)
```

Метод обновляет `parent_id`, `left` и `right` в памяти и в БД. После вызова нужно сделать `session.flush()` или `session.commit()`.

### 5. Удаление узла

При удалении через `session.delete()` разрыв закрывается автоматически, а дети удалённого узла **поднимаются на уровень выше**:

```python
# Было: root -> child -> sub
session.delete(child)  # sub поднимается к root
session.flush()       # Стало: root -> sub
```

Для каскадного удаления поддерева (удалить node + потомков) используйте `ns.delete()`:

```python
ns.delete(session, node, include_descendants=True)
session.flush()
```

### 6. Перестроение дерева

Если значения `left`/`right` повреждены (например, после массового импорта):

```python
ns.rebuild(session, Category)
session.commit()
```

## API

### NestedSetManager

| Метод | Описание |
|---|---|
| `register(model, parent_column)` | Зарегистрировать модель |
| `move(session, node, new_parent_id)` | Переместить узел к новому родителю |
| `delete(session, node, include_descendants)` | Удалить узел и закрыть разрыв |
| `rebuild(session, model)` | Перестроить nested set с нуля |

### Запросы

| Метод | Результат |
|---|---|
| `ancestors(session, node)` | Все предки (исключая сам узел) |
| `descendants(session, node, include_self)` | Все потомки |
| `children(session, node)` | Непосредственные дети |
| `subtree(session, node)` | Поддерево (включая узел) |
| `siblings(session, node)` | Соседние узлы |
| `depth(session, node)` | Глубина (0 для корня) |
| `get_roots(session, model)` | Корневые узлы |
| `get_tree(session, model)` | Все узлы, упорядоченные по left |
| `is_leaf(node)` | True если лист |
| `is_root(node)` | True если корень |

## Как это работает

- `before_insert` — находит точку вставки (справа от последнего потомка родителя), раздвигает `left`/`right`, назначает новому узлу `(parent_right, parent_right + 1)`.
- `before_delete` — читает актуальные `left`/`right` из БД (в обход кеша сессии), сдвигает все значения правее удаляемого диапазона влево на `width`.
- `move()` — закрывает разрыв на старом месте, открывает на новом, обновляет `left`/`right` узла.
- Все операции используют `SELECT ... FOR UPDATE` для избежания race condition.
