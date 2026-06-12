# sqlalchemy_nested_set — полная документация

Библиотека для автоматического управления Nested Set (MPTT) в моделях SQLAlchemy 2.0+.

---

## Содержание

1. [Что такое Nested Set](#1-что-такое-nested-set)
2. [Установка](#2-установка)
3. [Быстрый старт](#3-быстрый-старт)
4. [Определение модели](#4-определение-модели)
5. [Регистрация в менеджере](#5-регистрация-в-менеджере)
6. [Создание записей](#6-создание-записей)
7. [Перемещение узлов (move)](#7-перемещение-узлов-move)
8. [Удаление узлов](#8-удаление-узлов)
9. [Запросы](#9-запросы)
10. [Перестроение дерева (rebuild)](#10-перестроение-дерева-rebuild)
11. [Автоматическое обнаружение операций](#11-автоматическое-обнаружение-операций)
12. [Важные замечания](#12-важные-замечания)
13. [Полный пример с FastAPI](#13-полный-пример-с-fastapi)
14. [Алгоритм move (подробно)](#14-алгоритм-move-подробно)
15. [Справочник API](#15-справочник-api)

---

## 1. Что такое Nested Set

Nested Set — это способ хранения деревьев в реляционной БД с помощью двух колонок `left` и `right`. Каждый узел получает диапазон `[left, right)`, который заключает в себя диапазоны всех его потомков:

```
      root [1, 10]
      /    |     \
 child   other   sibling
[2,5]   [6,7]    [8,9]
  |
 sub
[3,4]
```

- `left`  — порядковый номер при обходе дерева в глубину (DFS, pre-order)
- `right` — номер при возврате из рекурсии
- Если `right == left + 1` — узел лист (нет потомков)

**Преимущества:** любые запросы к дереву выполняются за один SELECT без рекурсии.
**Недостатки:** вставка/удаление/перемещение требуют массового обновления left/right у соседних узлов.

Библиотека автоматизирует все операции с left/right.

---

## 2. Установка

```bash
pip install sqlalchemy-nested-set
```

Требования: Python >= 3.10, SQLAlchemy >= 2.0.

---

## 3. Быстрый старт

```python
from sqlalchemy import Column, Integer, String, ForeignKey, create_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy_nested_set import NestedSetMixin, NestedSetManager

class Base(DeclarativeBase):
    pass

class Category(NestedSetMixin, Base):
    __tablename__ = 'categories'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    parent_id = Column(Integer, ForeignKey('categories.id'))

engine = create_engine('sqlite:///tree.db')
Base.metadata.create_all(engine)

ns = NestedSetManager()
ns.register(Category, parent_column='parent_id')

session = Session(engine)

# Создание
root = Category(name='root')
session.add(root)
session.flush()

child = Category(name='child', parent_id=root.id)
session.add(child)
session.flush()

# Перемещение
ns.move(session, child, new_parent_id=None)  # в корень
session.flush()

# Удаление с авто-каскадом
session.delete(root)
session.flush()

session.close()
```

---

## 4. Определение модели

Модель должна наследовать `NestedSetMixin` и определить свою колонку `parent_id` (имя может быть любым):

```python
from sqlalchemy_nested_set import NestedSetMixin

class Category(NestedSetMixin, Base):
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    parent_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
```

`NestedSetMixin` добавляет две колонки:

```python
class NestedSetMixin:
    left = Column(Integer, nullable=False)
    right = Column(Integer, nullable=False)
```

Имя колонки-родителя (`parent_id`) может быть другим — укажите его при регистрации:

```python
class Page(NestedSetMixin, Base):
    __tablename__ = 'pages'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    parent_page_id = Column(Integer, ForeignKey('pages.id'))

ns.register(Page, parent_column='parent_page_id')
```

---

## 5. Регистрация в менеджере

```python
ns = NestedSetManager()
ns.register(Category, parent_column='parent_id')
```

Что происходит:
- Менеджер вешает обработчик `before_flush` на `Session`
- В `before_flush` обрабатываются `session.new` (insert), `session.deleted` (delete), `session.dirty` (move)
- Модель добавляется во внутренний реестр

Можно зарегистрировать несколько моделей:

```python
ns.register(Category, parent_column='parent_id')
ns.register(Page, parent_column='parent_page_id')
```

---

## 6. Создание записей

Все операции ниже работают **автоматически** — менеджер сам рассчитывает left/right.

### Корневой узел (без родителя)

```python
root = Category(name='root')
session.add(root)
session.flush()
# Таблица:
# id=1  root  parent_id=NULL  left=1  right=2
```

Узел вставляется справа от всех существующих корней.

### Дочерний узел

```python
child = Category(name='child', parent_id=root.id)
session.add(child)
session.flush()
# root.right раздвигается: left=1 → right=4
# child вставляется внутри root: left=2, right=3
```

### Вставка между существующими узлами

```python
sub = Category(name='sub', parent_id=child.id)
session.add(sub)
session.flush()
# child.right раздвигается: left=2 → right=5
# root.right раздвигается: left=1 → right=6
# sub: left=3, right=4
```

**Как это работает (`_process_inserts`):**
1. Читает `parent.right` из БД (с `FOR UPDATE`)
2. Раздвигает: `left >= parent.right → += 2`, `right >= parent.right → += 2`
3. Обновляет `parent.right` в памяти (только родитель, siblings stale)
4. Назначает узлу: `left = parent.right`, `right = parent.right + 1`

**Важно:** после flush left/right у соседних узлов в памяти могут быть устаревшими. Используйте `session.expire_all()` или `session.refresh(obj)` для получения актуальных значений.

---

## 7. Перемещение узлов (move)

### Явный вызов `ns.move()`

```python
ns.move(session, node, new_parent_id=target_parent_id)
session.flush()
# node.parent_id, node.left, node.right обновлены в БД

# Перемещение в корень (без родителя):
ns.move(session, node, new_parent_id=None)
session.flush()
```

Метод `ns.move()`:
- Принимает **ORM-объект** (не id)
- Принимает `new_parent_id` (int или None)
- Кидает `NestedSetError`, если:
  - Родитель не найден
  - Попытка переместить узел в самого себя

### Автоматическое перемещение (через смену parent_id)

```python
node.parent_id = new_parent_id
session.flush()
# left/right пересчитаны автоматически
```

Менеджер в `_process_moves` проверяет `inspect(obj).attrs[parent_id].history.has_changes()` и вызывает `_move_subtree`.

### Пример с таблицей

До:

```
id  name     parent_id  left  right
--  -------  ---------  ----  -----
 1  root     NULL         1     10
 2  child    1            2      5
 3  sub      2            3      4
 4  other    1            6      7
 5  sibling  1            8      9
```

```
       root [1, 10]
       /    |     \
  child   other  sibling
  [2,5]   [6,7]   [8,9]
    |
   sub
  [3,4]
```

```python
ns.move(session, sub, new_parent_id=sibling.id)
session.flush()
```

После:

```
id  name     parent_id  left  right
--  -------  ---------  ----  -----
 1  root     NULL         1     10
 2  child    1            2      3
 4  other    1            4      5
 5  sibling  1            6      9
 3  sub      5            7      8
```

```
       root [1, 10]
       /    |     \
   child  other   sibling
   [2,3]  [4,5]   [6,9]
                    |
                   sub
                  [7,8]
```

Подробнее об алгоритме — в разделе [14](#14-алгоритм-move-подробно).

---

## 8. Удаление узлов

### Удаление листа

```python
session.delete(leaf_node)
session.flush()
# Разрыв закрыт: left/right соседних узлов сдвинуты
```

### Удаление узла с потомками (авто-подъём детей)

При удалении узла его **потомки поднимаются на уровень выше** — становятся детьми родителя удалённого узла:

```python
session.delete(parent_node)
session.flush()
# Дети parent_node поднимаются к родителю parent_node
# left/right детей пересчитаны, разрыв закрыт
```

Пример:

```python
# Было: root[1,6], child[2,5], sub[3,4]
#        root
#        └── child
#            └── sub
session.delete(child)
session.flush()
# Стало: root[1,4], sub[2,3]
#        root
#        └── sub  (parent_id=root.id)
```

Если удаляется корень — дети становятся корнями:

```python
# Было: root[1,6], child[2,5], sub[3,4]
session.delete(root)
session.flush()
# Стало: child[1,4], sub[2,3]
#        child (parent_id=None)
#        └── sub (parent_id=child.id)
```

### Что происходит при auto-delete (`_process_deletes`)

1. Читает `left`/`right` из БД (с `FOR UPDATE`)
2. Если `right - left > 1` (есть дети):
   - `UPDATE SET parent_id = obj.parent_id WHERE parent_id = obj.id` — дети поднимаются к родителю удалённого
   - `UPDATE SET left -= 1, right -= 1 WHERE left > l1 AND right < r1` — все потомки сдвигаются влево (удалён entry узла)
3. Закрывает разрыв: `left > r1 → -= 2`, `right > r1 → -= 2` (на ширину самого узла, а не поддерева)

### Явный вызов `ns.delete()` (каскадное удаление)

```python
ns.delete(session, node, include_descendants=True)
session.flush()
# Удаляет node + всех потомков каскадно (через ORM .delete())
# Разрыв закрывается в _process_deletes через обработчик before_flush
```

Используйте `ns.delete()`, когда нужно удалить поддерево целиком, а не поднимать детей на уровень выше.

---

## 9. Запросы

Все запросы выполняются за один SELECT и возвращают итератор объектов модели.

### ancestors — предки

```python
ancestors = ns.ancestors(session, node)
# SELECT * FROM categories
# WHERE left < node.left AND right > node.right
# ORDER BY left
# Возвращает от корня до родителя (исключая сам узел)
```

### descendants — потомки

```python
descendants = ns.descendants(session, node)
# SELECT * FROM categories
# WHERE left > node.left AND right < node.right
# ORDER BY left

descendants_incl = ns.descendants(session, node, include_self=True)
# Включает сам узел
```

### children — непосредственные дети

```python
children = ns.children(session, node)
# SELECT * FROM categories WHERE parent_id = node.id
# Использует parent_id, не left/right
```

### subtree — поддерево

```python
subtree = ns.subtree(session, node)
# SELECT * FROM categories
# WHERE left >= node.left AND right <= node.right
# ORDER BY left
# Включает сам узел
```

### siblings — соседи

```python
siblings = ns.siblings(session, node)
# SELECT * FROM categories
# WHERE parent_id = node.parent_id AND id != node.id
```

### depth — глубина

```python
depth = ns.depth(session, node)
# SELECT count(*) FROM categories
# WHERE left < node.left AND right > node.right
# 0 для корня, 1 для его детей, и т.д.
```

### is_leaf / is_root

```python
ns.is_leaf(node)  # right == left + 1
ns.is_root(node)  # parent_id is None
```

### get_roots — корни

```python
roots = ns.get_roots(session, Category)
# SELECT * FROM categories WHERE parent_id IS NULL ORDER BY left
```

### get_tree — все узлы

```python
tree = ns.get_tree(session, Category)
# SELECT * FROM categories ORDER BY left
```

---

## 10. Перестроение дерева (rebuild)

После массового импорта или ручных манипуляций left/right могут быть повреждены.

```python
ns.rebuild(session, Category)
session.commit()
```

Алгоритм:
1. Загружает все узлы, группирует по `parent_id`
2. Рекурсивно обходит дерево (DFS)
3. Назначает `left`/`right` каждому узлу в памяти
4. Вызывает `session.flush()` для записи в БД

---

## 11. Автоматическое обнаружение операций

Менеджер перехватывает `before_flush` и автоматически определяет тип операции для каждой модели из реестра:

| Состояние объекта | Операция | Метод |
|---|---|---|
| `session.new` | Вставка | `_process_inserts` |
| `session.deleted` | Удаление | `_process_deletes` (+ подъём детей на уровень выше) |
| `session.dirty` (parent_id изменился) | Перемещение | `_process_moves` → `_move_subtree` |

Таким образом, пользователь может работать с сессией стандартными средствами SQLAlchemy:

```python
# Создание (add → insert)
cat = Category(name='new', parent_id=root.id)
session.add(cat)
session.flush()  # left/right рассчитаны

# Перемещение (смена parent_id → move)
cat.parent_id = other.id
session.flush()  # поддерево перемещено

# Удаление (delete → подъём детей на уровень выше)
session.delete(cat)
session.flush()  # дети cat подняты к родителю, разрыв закрыт
```

Явные методы (`ns.move`, `ns.delete`) нужны для:
- Валидации (проверка существования родителя, запрет перемещения в себя)
- Когда требуется явный контроль над операцией

---

## 12. Важные замечания

### Согласованность сессии

После `flush()` left/right в памяти могут быть устаревшими для объектов, не участвовавших в операции. Например, при вставке дочернего узла `parent.right` обновляется в памяти, но siblings остаются stale.

```python
# Получить актуальные значения:
session.refresh(obj)
# или:
session.expire_all()
```

### Stale-данные в сессии

После `flush()` left/right соседних узлов могут быть устаревшими в памяти. Всегда используйте `session.expire_all()` или `session.refresh(obj)` перед чтением left/right после массовых операций.

### SQLite и FOR UPDATE

SQLite игнорирует `SELECT ... FOR UPDATE` (не поддерживает блокировки строк). Тесты на SQLite корректны, но блокировки работают только на PostgreSQL/MySQL.

### Identity map warning

SAWarning об identity map при загрузке объектов внутри event handler — ожидаемое поведение, не влияет на корректность.

---

## 13. Полный пример с FastAPI

```python
# app/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session

engine = create_engine('sqlite:///nested_set_demo.db', echo=True)

class Base(DeclarativeBase):
    pass

def get_session():
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

```python
# app/models.py
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy_nested_set import NestedSetMixin
from app.database import Base

class Category(NestedSetMixin, Base):
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    parent_id = Column(Integer, ForeignKey('categories.id'), nullable=True)

    parent = relationship('Category', remote_side='Category.id', backref='children')
```

```python
# app/nested_set.py
from sqlalchemy_nested_set import NestedSetManager
from app.models import Category

ns = NestedSetManager()
ns.register(Category, parent_column='parent_id')
```

```python
# app/schemas.py
from pydantic import BaseModel
from typing import Optional

class CategoryCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None

class CategoryMove(BaseModel):
    new_parent_id: Optional[int] = None

class CategoryOut(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    left: int
    right: int

    class Config:
        from_attributes = True
```

```python
# app/crud.py
from sqlalchemy.orm import Session
from app.models import Category
from app.nested_set import ns
from typing import List, Optional

def create_category(session: Session, name: str, parent_id: Optional[int] = None) -> Category:
    cat = Category(name=name, parent_id=parent_id)
    session.add(cat)
    session.flush()
    session.refresh(cat)
    return cat

def move_category(session: Session, cat: Category, new_parent_id: Optional[int]) -> Category:
    ns.move(session, cat, new_parent_id=new_parent_id)
    session.flush()
    session.refresh(cat)
    return cat

def delete_category(session: Session, cat: Category):
    session.delete(cat)
    # auto-cascade удалит потомков
    session.flush()
```

```python
# app/main.py
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import engine, Base, get_session
from app.models import Category
from app.schemas import CategoryCreate, CategoryMove, CategoryOut
from app.crud import create_category, move_category, delete_category

Base.metadata.create_all(engine)

app = FastAPI(title='Nested Set Demo')

@app.post('/categories', response_model=CategoryOut)
def create(body: CategoryCreate, session: Session = Depends(get_session)):
    cat = create_category(session, body.name, body.parent_id)
    return cat

@app.get('/categories', response_model=list[CategoryOut])
def list_all(session: Session = Depends(get_session)):
    return ns.get_tree(session, Category)

@app.get('/categories/roots', response_model=list[CategoryOut])
def roots(session: Session = Depends(get_session)):
    return list(ns.get_roots(session, Category))

@app.get('/categories/{cat_id}/ancestors', response_model=list[CategoryOut])
def ancestors(cat_id: int, session: Session = Depends(get_session)):
    cat = session.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    return list(ns.ancestors(session, cat))

@app.get('/categories/{cat_id}/children', response_model=list[CategoryOut])
def children(cat_id: int, session: Session = Depends(get_session)):
    cat = session.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    return list(ns.children(session, cat))

@app.patch('/categories/{cat_id}/move', response_model=CategoryOut)
def move(cat_id: int, body: CategoryMove, session: Session = Depends(get_session)):
    cat = session.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    return move_category(session, cat, body.new_parent_id)

@app.delete('/categories/{cat_id}')
def delete(cat_id: int, session: Session = Depends(get_session)):
    cat = session.get(Category, cat_id)
    if not cat:
        raise HTTPException(404)
    delete_category(session, cat)  # auto: дети поднимаются к родителю
    return {'ok': True}

@app.post('/categories/rebuild')
def rebuild(session: Session = Depends(get_session)):
    ns.rebuild(session, Category)
    return {'ok': True}
```

```bash
# run.py
import uvicorn
uvicorn.run('app.main:app', host='0.0.0.0', port=8000, reload=True)
```

```bash
# Тестирование
curl -X POST 'http://localhost:8000/categories?name=root'
curl -X POST 'http://localhost:8000/categories?name=child&parent_id=1'
curl -X PATCH 'http://localhost:8000/categories/2/move' -H 'Content-Type: application/json' -d '{"new_parent_id": null}'
curl -X GET 'http://localhost:8000/categories'
curl -X DELETE 'http://localhost:8000/categories/1'
```

---

## 14. Алгоритм move (подробно)

`_move_subtree` выполняет 3 шага:

### Шаг 1: Закрыть разрыв на старом месте

```
id  name     left  right
--  -------  ----  -----
 1  root       1     10
 2  child      2      5
 3  sub        3      4     ← хотим переместить sub
 4  other      6      7
 5  sibling    8      9
```

Все узлы НЕ из поддерева sub, у которых `left > 4` или `right > 4`, сдвигаются влево на `width` (2):

```
id  name     left  right
--  -------  ----  -----
 1  root       1      8   (10 → 8)
 2  child      2      3   (5 → 3)
 4  other      4      5   (6→4, 7→5)
 5  sibling    6      7   (8→6, 9→7)
```

### Шаг 2: Открыть разрыв на новом месте

`target_left = parent.right` (или `max(right) + 1` для корня).

Все узлы НЕ из поддерева, у которых `left >= target_left` или `right >= target_left`, сдвигаются вправо на `width`:

```
id  name     left  right
--  -------  ----  -----
 1  root       1     10   (8 → 10)
 2  child      2      3
 4  other      4      5
 5  sibling    6      9   (7 → 9)
```

### Шаг 3: Сдвинуть поддерево

`delta = target_left - l1`. Добавляем `delta` к `left` и `right` всех узлов поддерева:

```
sub: left=7, right=8  (было 3, 4; delta = 7 - 3 = 4)
```

Результат:

```
id  name     parent_id  left  right
--  -------  ---------  ----  -----
 1  root     NULL         1     10
 2  child    1            2      3
 4  other    1            4      5
 5  sibling  1            6      9
 3  sub      5            7      8
```

### ID-based трекинг поддерева

При move поддерево идентифицируется по **ID** (WHERE id IN (...)), а не по диапазону left/right. Это важная деталь: во время закрытия разрыва left/right соседних узлов меняются, и фильтрация по left/right могла бы захватить чужие узлы. ID-трекинг этого избегает.

---

## 15. Справочник API

### NestedSetManager

| Метод | Сигнатура | Описание |
|---|---|---|
| `register` | `(model, parent_column='parent_id')` | Зарегистрировать модель с именем колонки-родителя |
| `move` | `(session, node, new_parent_id)` | Переместить узел к новому родителю |
| `delete` | `(session, node, include_descendants=True)` | Удалить узел + потомки (явный вызов) |
| `rebuild` | `(session, model)` | Перестроить left/right с нуля по parent_id |

### Запросы

| Метод | Результат | SQL-фильтр |
|---|---|---|
| `ancestors(session, node)` | Предки (без узла) | `left < L AND right > R ORDER BY left` |
| `descendants(session, node, include_self=False)` | Потомки | `left > L AND right < R ORDER BY left` |
| `children(session, node)` | Непосредственные дети | `parent_id = node.id` |
| `subtree(session, node)` | Поддерево (включая узел) | `left >= L AND right <= R ORDER BY left` |
| `siblings(session, node)` | Соседи | `parent_id = node.parent_id AND id != node.id` |
| `depth(session, node)` | Глубина (int) | `SELECT count(*) WHERE left < L AND right > R` |
| `get_roots(session, model)` | Корневые узлы | `parent_id IS NULL ORDER BY left` |
| `get_tree(session, model)` | Все узлы | `ORDER BY left` |
| `is_leaf(node)` | bool | `right == left + 1` |
| `is_root(node)` | bool | `parent_id IS NULL` |

### NestedSetError

```python
class NestedSetError(Exception):
    pass
```

Выбрасывается при:
- Обращении к незарегистрированной модели
- `move()` с несуществующим parent_id
- `move()` с parent_id == node.id (сам в себя)

---

*Дата: июнь 2026*
