activation activate() {
  // take advisory lock
  db.execute("SELECT pg_advisory_lock(1)")  // or db.query
  // await migrations job
  await Job.run("migrations")
  // emit INSERT into migration_log
  db.execute("INSERT INTO migration_log ...")
}
