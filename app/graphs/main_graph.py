from typing import Dict, Any, Literal, TypedDict
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables.config import RunnableConfig
from ..schemas.state import State
from ..services.llm import llm_light
from ..logger import setup_logger
from langchain_core.output_parsers import StrOutputParser
from ..database.vector_store import get_vectorstore

logger = setup_logger(__name__)

graph = StateGraph(State)

general_answer = """Данный вопрос определен как не связанный с руководством пользователя, пожалуйста попробуйте снова или обратитесь в наш отдел технической поддержки."""

async def classify_index(state:State) -> State:  # -> Literal["index", "general"]:
    with open("index_summary.txt", "r") as f:
        index = f.read()
    prompt_template = """
Ты - оператор чат-бота, задача которого отвечать на вопросы пользователей, связанные с руководством по приложению. Тебе будет дан текущий запрос пользователя и выжимка из руководства пользователя. Ты должен вернуть ответ строго одним словом: 'Да' или 'Нет'.

Если вопрос пользователя **связан с программой или руководством**, ответь 'Да'.
Если вопрос пользователя **не связан с программой или руководством** (отвлечённая тема), ответь 'Нет'.

**Примеры:**

Выжимка из руководства:

{summary}

Последние сообщения пользователя:

{user_context}

Текущий вопрос пользователя:

1. "Как добавить нового пользователя в систему?"
**Ответ:** Да

2. "Какая погода сегодня?"
**Ответ:** Нет

3. "Как настроить аудит конфигурации ПО?"
**Ответ:** Да

Теперь обработай текущий запрос.

Текущий вопрос пользователя:

{user_message}

Ответ:
"""

    prompt = PromptTemplate.from_template(prompt_template)
    bound = prompt | llm_light | StrOutputParser()
    response:str = await bound.ainvoke({
        "summary":index,
        "user_context":"\n".join([el[1] for el in state["last_messages"] if el[0] == "user"]),
        "user_message":state["user_message"]
    })
    if response.replace("'", "").lower() == "нет" or response.replace("'", "").lower().endswith("нет"):
        return {"is_index":False, "answer":general_answer, "rel_docs":[]}
    else:
        return {"is_index":True}

def route_index(state:State) -> Literal["get_relevant_docs", "__end__"]:
    if state["is_index"]:
        return "get_relevant_docs"
    else:
        return END

# doc_schema:
# {"1.1.1(.1)":{"text":str, "images":list[str]("paths")}}
# {"1.1.1.1":Document()}

async def get_relevant_docs(state:State) -> State:
    vectorstore = get_vectorstore()
    return {'rel_docs':[{el.metadata["chapter"]:{"text":el.metadata["content"], "images":el.metadata["paths"]}} for el in await vectorstore.asimilarity_search(query=state["db_query"])]}

async def score_docs(state:State)->State:
    new_rel_docs=[]
    prompt_template = """
Ты - оператор чат-бота помощника по руководству пользователя. Твоя задача по предоставленному запросу пользователя определить, является ли предложенный раздел документации подходящим.

Отвечай 'Да' если документ релевантен запросу и его можно учитывать при генерации ответа.
Отвечай 'Нет' если документ не поможет пользователю в решении вопроса или он второстепенный.

Важно:

- **Основывайся только на предоставленных данных.**
- **Не добавляй дополнительной информации.**
- **Избегай галлюцинаций.**

**Примеры:**

Текущий вопрос пользователя:

"Как удалить профиль из системы?"

Документ на оценку:

"1.12.9: Удаление профиля - Описывает процесс удаления профиля из системы."

**Ответ:** Да

---

Текущий вопрос пользователя:

"Как изменить настройки браузера?"

Документ на оценку:

"1.6: Авторизация - Описывает процесс входа в систему."

**Ответ:** Нет

Теперь оцени следующий документ.

Текущий вопрос пользователя:

{user_message}

Документ на оценку:

{doc_check}

Ответ:
"""

    prompt = PromptTemplate.from_template(prompt_template)
    bound = prompt | llm_light | StrOutputParser()
    for doc in state["rel_docs"]:
        response = await bound.ainvoke({
            "user_message":state["user_message"],
            "doc_check":list(doc.values())[0]["text"]
        })
        if response.replace("'", "").lower() == "нет" or response.replace("'", "").lower().endswith("нет"):
            continue
        else:
            new_rel_docs.append(doc)
    if len(new_rel_docs) == 0:
        return {"rewrite":True, "rel_docs":[], "retries":state['retries'] + 1}
    else:
        return {"rewrite":False, "rel_docs":new_rel_docs}

contact_message = """Ответ на ваш вопрос не найден среди документов руководства пользователя. Если Вам требуется квалифицированная помощь, позвоните на телефон «горячей линии поддержки», напишите письмо или воспользуйтесь формой регистрации заявки на сайте. 
КОНТАКТНАЯ ИНФОРМАЦИЯ
Техническая поддержка
+7 (495) 258-06-36
info@lense.ru
lense.ru
"""

def route_docs(state:State) -> Literal["rewrite_query", "no_docs", "generate"]:
    if state["retries"] >= 2:
        return "no_docs"
    elif state["rewrite"]:
        return "rewrite_query"
    else:
        return "generate"


async def rewrite_query(state:State) -> State:
    prompt_template = """
Ты - оператор чат-бота помощника по использованию системы управления безопасностью конфигураций ПО. Твоя задача - перефразировать запрос пользователя так, чтобы он привёл к более точным результатам поиска в базе данных.

Важно:

- **Используй термины из руководства пользователя.**
- **Не добавляй информацию, отсутствующую в исходном запросе.**
- **Избегай галлюцинаций.**

**Примеры:**

Краткая выжимка из руководства:

{summary}

Сообщение пользователя:

"Как удалить профиль?"

Неудавшийся запрос:

"Удаление аккаунта"

**Перефразированный запрос:**

"Удаление профиля из системы"

---

Теперь перефразируй текущий запрос.

Сообщение пользователя:

{user_message}

Неудавшийся запрос:

{last_query}

Ответ:
"""

    prompt = PromptTemplate.from_template(prompt_template)
    bound = prompt | llm_light | StrOutputParser()
    with open("index_summary.txt", "r") as f:
        index = f.read()
    return {"db_query": await bound.ainvoke({"summary": index, "user_message":state['user_message'], "last_query":state["db_query"]})}


def no_docs(state:State) -> State:
    return {"answer":contact_message}

async def generate(state:State)-> State:
    prompt_template = """
Ты - чат-бот, цель которого помочь пользователю в использовании платформы. Тебе будут предоставлены история чата, текущий вопрос пользователя и несколько релевантных документов из руководства пользователя. Твоя задача - использовать этот контекст для ответа на вопрос пользователя.

При необходимости, можешь упомянуть изображения, включая их названия или описания, но не придумывай их.

Важно:

- **Основывайся только на предоставленных документах.**
- **Не добавляй информацию, отсутствующую в документах.**
- **Избегай галлюцинаций.**

**Шаблон сообщения для направления пользователя в техподдержку при возникших трудностях:**

{contact_message}

**Релевантные документы:**

{rel_docs}

**Последние сообщения пользователя:**

{user_context}

**Текущий вопрос пользователя:**

{user_message}

Ответ:
"""

    prompt = PromptTemplate.from_template(prompt_template)
    bound = prompt | llm_light | StrOutputParser()
    response:str = await bound.ainvoke({
        "contact_message":contact_message,
        "rel_docs":"\n\n".join([list(el.values())[0]["text"] for el in state['rel_docs']]),
        "user_context":"\n".join([f"{el[0]}: {el[1]}" for el in state["last_messages"]]),
        "user_message":state["user_message"]
    })
    return {"answer":response}

async def score_answer(state:State) -> State:
    prompt_template = """
Ты - оператор чат-бота, задача которого оценить сгенерированный ответ на запрос пользователя. Ты должен вернуть ответ строго одним словом: 'Да' или 'Нет'.

Если ответ чат-бота **удовлетворяет запрос пользователя**, ответь 'Да'.
Если ответ **не решает проблему пользователя или содержит ошибки**, ответь 'Нет'.

**Примеры:**

Релевантные документы:

[Документы, связанные с запросом]

Текущий вопрос пользователя:

"Как добавить новый шаблон конфигурации?"

Ответ чат-бота на оценку:

"Чтобы добавить новый шаблон, перейдите в раздел 'Управление шаблонами' и нажмите 'Создать шаблон'."

**Твоя оценка:** Да

---

Текущий вопрос пользователя:

"Как выключить компьютер?"

Ответ чат-бота на оценку:

"Вы можете выключить компьютер через меню 'Пуск' или нажать кнопку питания."

**Твоя оценка:** Нет

Теперь оцени текущий ответ.

Релевантные документы:

{rel_docs}

Текущий вопрос пользователя:

{user_message}

Ответ чат-бота на оценку:

{answer}

Твоя оценка:
"""

    prompt = PromptTemplate.from_template(prompt_template)
    bound = prompt | llm_light | StrOutputParser()
    response:str = await bound.ainvoke({
        "rel_docs":"\n\n".join([list(el.values())[0]["text"] for el in state['rel_docs']]),
        "user_context":"\n".join([f"{el[0]}: {el[1]}" for el in state["last_messages"]]),
        "user_message":state["user_message"],
        "answer":state["answer"]
    })
    if response.replace("'", "").lower() == "нет" or response.replace("'", "").lower().endswith("нет"):
        return {"rewrite":True, "answer":contact_message, "rel_docs":[],"retries":state["retries"] + 1}
    else:
        return {"rewrite":False}

def route_answer(state:State) -> Literal["rewrite_query", "__end__"]:
    if state["rewrite"]:
        return "rewrite_query"
    return END

graph.add_node(classify_index)
graph.add_node(get_relevant_docs)
graph.add_node(score_docs)
graph.add_node(rewrite_query)
graph.add_node(no_docs)
graph.add_node(generate)
graph.add_node(score_answer)
graph.set_entry_point("classify_index")
graph.add_conditional_edges("classify_index", route_index)
graph.add_edge("get_relevant_docs", "score_docs")
graph.add_conditional_edges("score_docs", route_docs)
graph.add_edge("rewrite_query", "get_relevant_docs")
graph.add_edge("generate", "score_answer")
graph.set_finish_point("no_docs")
graph.add_conditional_edges("score_answer", route_answer)
worker = graph.compile()