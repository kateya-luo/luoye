#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "agenda_protocol.h"

static int failures;
#define CHECK(x) do { if (!(x)) { \
  printf("FAIL line %d: %s\n", __LINE__, #x); failures++; \
} } while (0)

int main(void) {
  char text[32];
  CHECK(luoye_agenda_text(text, sizeof(text), "学生会"));
  CHECK(strcmp(text, "学生会") == 0);
  CHECK(!luoye_agenda_text(text, sizeof(text), "bad\ntext"));
  CHECK(!luoye_agenda_text(text, 4, "学生会"));
  CHECK(!luoye_agenda_text(text, sizeof(text), "\xF0\x9F\x98\x80"));
  CHECK(!luoye_agenda_text(text, sizeof(text), "\xED\xA0\x80"));

  CHECK(luoye_agenda_accept(4, 7, 5, 7));
  CHECK(!luoye_agenda_accept(5, 7, 5, 7));
  CHECK(!luoye_agenda_accept(5, 7, 6, 0));
  CHECK(luoye_agenda_accept(99, 7, 1, 8));

  luoye_agenda_snapshot_t agenda = {0};
  agenda.count = 4;
  agenda.items[0].reminder_utc = 1200;
  agenda.items[1].reminder_utc = 1100;
  agenda.items[2].reminder_utc = 1050;
  agenda.items[2].dismissed = true;
  agenda.items[3].reminder_utc = 900;
  CHECK(luoye_agenda_next_index(&agenda, 1000) == 1);
  CHECK(luoye_agenda_due_index(&agenda, 1045, 59) == 3);
  agenda.items[3].dismissed = true;
  CHECK(luoye_agenda_due_index(&agenda, 1045, 10) == -1);
  CHECK(luoye_agenda_due_index(&agenda, 1050, 50) == 1);

  char query[40];
  CHECK(luoye_agenda_query(query, sizeof(query), 42));
  CHECK(strcmp(query, "?after_revision=42") == 0);
  CHECK(!luoye_agenda_query(query, 8, 42));

  printf(failures ? "%d agenda checks failed\n" : "agenda checks passed\n",
         failures);
  return failures ? 1 : 0;
}
