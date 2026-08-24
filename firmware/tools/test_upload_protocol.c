#include <stdio.h>
#include <string.h>
#include "upload_protocol.h"

static int failures;
#define CHECK(x) do { if (!(x)) { \
  printf("FAIL line %d: %s\n", __LINE__, #x); failures++; \
} } while (0)

int main(void) {
  luoye_upload_chunk_t chunk;
  /* New live sessions use one second-ish 32 KiB durable chunks. */
  CHECK(luoye_upload_plan_chunk(100000, 0, 0, 32768, &chunk));
  CHECK(chunk.seq == 0 && chunk.offset == 0 && chunk.length == 32768);
  CHECK(luoye_upload_ack_progress_valid(&chunk, 100000, 32768,
                                        1, 32768));
  CHECK(luoye_upload_plan_chunk(100000, 98304, 3, 32768, &chunk));
  CHECK(chunk.seq == 3 && chunk.offset == 98304 && chunk.length == 1696);

  /* Legacy sessions retain their original 160 KiB sequence geometry. */
  CHECK(luoye_upload_plan_chunk(400000, 0, 0, 163840, &chunk));
  CHECK(chunk.seq == 0 && chunk.offset == 0 && chunk.length == 163840);
  CHECK(luoye_upload_ack_valid(&chunk, 1, 163840));
  CHECK(!luoye_upload_ack_valid(&chunk, 1, 163839));
  CHECK(luoye_upload_ack_progress_valid(&chunk, 400000, 163840,
                                        2, 327680));
  CHECK(!luoye_upload_ack_progress_valid(&chunk, 400000, 163840,
                                         2, 327679));
  CHECK(luoye_upload_plan_chunk(400000, 327680, 2, 163840, &chunk));
  CHECK(chunk.seq == 2 && chunk.offset == 327680 && chunk.length == 72320);
  CHECK(!luoye_upload_plan_chunk(400000, 400000, 3, 163840, &chunk));
  CHECK(!luoye_upload_plan_chunk(10, 11, 0, 163840, &chunk));

  /* Exact chunk boundary still requires a separate final request. */
  CHECK(luoye_upload_plan_chunk(163840, 0, 0, 163840, &chunk));
  CHECK(chunk.length == 163840);
  CHECK(!luoye_upload_plan_chunk(163840, 163840, 1, 163840, &chunk));
  uint32_t acknowledged = 0;
  CHECK(luoye_upload_progress_from_samples(400000, 163840, 2, 163840,
                                           &acknowledged));
  CHECK(acknowledged == 327680);
  CHECK(!luoye_upload_progress_from_samples(400000, 163840, 1, 163840,
                                            &acknowledged));

  CHECK(luoye_upload_classify_http(true, 204) == LUOYE_UPLOAD_HTTP_OK);
  CHECK(luoye_upload_classify_http(true, 401) == LUOYE_UPLOAD_HTTP_AUTH);
  CHECK(luoye_upload_classify_http(true, 409) == LUOYE_UPLOAD_HTTP_CONFLICT);
  CHECK(luoye_upload_classify_http(true, 413) == LUOYE_UPLOAD_HTTP_PERMANENT);
  CHECK(luoye_upload_classify_http(true, 429) == LUOYE_UPLOAD_HTTP_RETRY);
  CHECK(luoye_upload_classify_http(true, 503) == LUOYE_UPLOAD_HTTP_RETRY);
  CHECK(luoye_upload_classify_http(false, 0) == LUOYE_UPLOAD_HTTP_RETRY);
  CHECK(luoye_upload_retry_delay_ms(0, 0) == 3000);
  CHECK(luoye_upload_retry_delay_ms(100, 0) <= 300000);

  char key[192];
  const char *hash = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  CHECK(luoye_upload_create_key(key, sizeof(key), "session-1"));
  CHECK(strcmp(key, "session:session-1:create") == 0);
  CHECK(luoye_upload_chunk_key(key, sizeof(key), "session-1", 7, hash));
  CHECK(strstr(key, "session:session-1:audio:7:") == key);
  CHECK(luoye_upload_mark_key(key, sizeof(key), "session-1", "mark-000001"));
  CHECK(strcmp(key, "session:session-1:mark:mark-000001") == 0);
  CHECK(luoye_upload_final_key(key, sizeof(key), "session-1"));
  CHECK(strcmp(key, "session:session-1:end") == 0);
  CHECK(luoye_upload_safe_path_id("server_01.A-b"));
  CHECK(!luoye_upload_safe_path_id("../session"));
  CHECK(!luoye_upload_safe_path_id("session/other"));

  /* Total JSONL size is unbounded: stream hundreds of individually bounded
     records through a small line buffer. */
  FILE *marks = tmpfile();
  CHECK(marks != NULL);
  if (marks) {
    for (int i = 0; i < 700; ++i) {
      fprintf(marks, "{\"kind\":\"important\",\"at_ms\":%d}\n", i);
    }
    rewind(marks);
    char line[96];
    uint32_t line_number = 0;
    size_t length = 0;
    int ready = 0;
    luoye_upload_mark_read_t result;
    while ((result = luoye_upload_read_mark_line(
                marks, line, sizeof(line), &line_number, &length)) !=
           LUOYE_UPLOAD_MARK_EOF) {
      CHECK(result == LUOYE_UPLOAD_MARK_READY);
      CHECK(length > 0 && line[0] == '{');
      ready++;
    }
    CHECK(ready == 700 && line_number == 700);
    fclose(marks);
  }

  /* One overlong record is consumed once, then the following line remains
     independently addressable as mark-000002. */
  marks = tmpfile();
  CHECK(marks != NULL);
  if (marks) {
    for (int i = 0; i < 20000; ++i) fputc('A', marks);
    fputs("\n{\"kind\":\"fav\",\"at_ms\":12}\n", marks);
    rewind(marks);
    char line[128];
    uint32_t line_number = 0;
    size_t length = 0;
    CHECK(luoye_upload_read_mark_line(marks, line, sizeof(line),
                                      &line_number, &length) ==
          LUOYE_UPLOAD_MARK_SKIPPED);
    CHECK(line_number == 1);
    CHECK(luoye_upload_read_mark_line(marks, line, sizeof(line),
                                      &line_number, &length) ==
          LUOYE_UPLOAD_MARK_READY);
    CHECK(line_number == 2 && strstr(line, "\"fav\"") != NULL);
    fclose(marks);
  }

  printf(failures ? "%d upload-protocol checks failed\n"
                  : "upload-protocol checks passed\n", failures);
  return failures ? 1 : 0;
}
