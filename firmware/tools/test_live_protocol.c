#include <stdio.h>
#include <string.h>

#include "live_protocol.h"

static int failures;
#define CHECK(x) do { if (!(x)) { \
  printf("FAIL line %d: %s\n", __LINE__, #x); failures++; \
} } while (0)

int main(void) {
  CHECK(luoye_live_cursor_accept(0, 0, 163840, 1, 163840));
  CHECK(luoye_live_cursor_accept(8, 300000, 327680, 9, 320000));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 8, 327680));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 7, 327680));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 9, 327681));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 9, 299999));

  char text[32];
  CHECK(luoye_live_set_text(text, sizeof(text), "hello"));
  CHECK(strcmp(text, "hello") == 0);
  CHECK(luoye_live_set_text(text, sizeof(text), "字幕"));
  CHECK(!luoye_live_set_text(text, 4, "字幕"));
  CHECK(!luoye_live_set_text(text, sizeof(text), "line\nbreak"));
  CHECK(!luoye_live_set_text(text, sizeof(text), "\xF0\x9F\x98\x80"));
  CHECK(!luoye_live_set_text(text, sizeof(text), "\xE4\xB8"));

  char rolling[10] = "one";
  CHECK(luoye_live_append_text(rolling, sizeof(rolling), "two"));
  CHECK(strcmp(rolling, "one two") == 0);
  CHECK(luoye_live_append_text(rolling, sizeof(rolling), "xyz"));
  CHECK(strcmp(rolling, "two xyz") == 0);
  char utf8_rolling[10] = "";
  CHECK(luoye_live_append_text(utf8_rolling, sizeof(utf8_rolling),
      "\xE4\xB8\x80\xE4\xBA\x8C\xE4\xB8\x89\xE5\x9B\x9B"));
  CHECK(strcmp(utf8_rolling,
      "\xE4\xBA\x8C\xE4\xB8\x89\xE5\x9B\x9B") == 0);
  CHECK(!luoye_live_append_text(utf8_rolling, sizeof(utf8_rolling),
                                "line\nbreak"));

  char query[48];
  CHECK(luoye_live_query(query, sizeof(query), 4294967295U));
  CHECK(strcmp(query, "?after_revision=4294967295") == 0);
  CHECK(!luoye_live_query(query, 8, 100));

  printf(failures ? "%d live-protocol checks failed\n"
                  : "live-protocol checks passed\n", failures);
  return failures ? 1 : 0;
}
